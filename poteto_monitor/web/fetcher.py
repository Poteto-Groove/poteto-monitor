"""データソースごとの取得スケジューラ（常駐モード用）。

- ソースごとに取得間隔を持ち、期限が来たソースだけを並列に取得する（E-1, E-3, E-5）。
- 失敗はソース単位・銘柄単位で切り分け、最後に取れた値を「古い値」として残す（B-6）。
- 429 は Retry-After に従い、その他の失敗は指数バックオフで待つ（E-9）。
- HTTP 接続はソースごとの requests.Session で使い回す（E-4）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from ..config import Config
from ..models import Asset, Reading
from ..providers import SOURCES, RateLimitError, Source, SourceResult, group_by_source

log = logging.getLogger("poteto-monitor.fetcher")

BACKOFF_BASE = 30  # 秒。連続失敗 1 回目の待ち時間（以降 2 倍ずつ）
BACKOFF_MAX = 600  # 秒。指数バックオフの上限
RATE_LIMIT_WAIT = 600  # 秒。429 で Retry-After が無いときの待ち時間


@dataclass
class FetchOutcome:
    readings: list[Reading]  # 設定順。取得できなかった銘柄は直近の値（あれば）
    fresh: set[str]  # この回に新しく取得できた Asset.key
    errors: dict[str, str]  # 直近の取得に失敗している Asset.key -> 理由


@dataclass
class _SourceState:
    signature: tuple = ()
    next_due: float = 0.0
    failures: int = 0
    readings: dict[str, Reading] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class SourceScheduler:
    def __init__(
        self,
        sources: dict[str, Source] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sources = SOURCES if sources is None else sources
        self._clock = clock
        self._states: dict[str, _SourceState] = {}
        self._sessions: dict[str, requests.Session] = {}

    def _session(self, name: str) -> requests.Session:
        # Session はスレッド安全が保証されないため、並列に動くソースごとに分ける。
        if name not in self._sessions:
            self._sessions[name] = requests.Session()
        return self._sessions[name]

    def close(self) -> None:
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()

    def _fetch_one(self, name: str, assets: list[Asset], cfg: Config) -> SourceResult:
        api_key = cfg.coingecko_api_key if name == "coingecko" else ""
        return self.sources[name].fetch(assets, cfg.base_currency, session=self._session(name), api_key=api_key)

    def _schedule(self, name: str, state: _SourceState, cfg: Config, now: float, result: SourceResult) -> None:
        delay = float(cfg.intervals.get(name, 0))
        if result.next_update is not None:
            # 上流の次回更新まで取りに行かない（少し余裕を持たせる）。
            delay = max(delay, result.next_update - now + 60)
        state.next_due = now + delay

    def _backoff(self, name: str, state: _SourceState, cfg: Config, now: float, exc: Exception) -> None:
        state.failures += 1
        delay = max(float(cfg.intervals.get(name, 0)), min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (state.failures - 1)))
        if isinstance(exc, RateLimitError):
            delay = max(delay, exc.retry_after or RATE_LIMIT_WAIT)
        state.next_due = now + delay
        log.error("%s の取得に失敗（%d 回連続, %.0f 秒後に再試行）: %s", name, state.failures, delay, exc)

    async def fetch(self, cfg: Config) -> FetchOutcome:
        groups = group_by_source(cfg.assets, self.sources)
        now = self._clock()

        # 監視対象から外れたソースは状態ごと捨てる。
        for name in list(self._states):
            if name not in groups:
                del self._states[name]

        due: list[tuple[str, list[Asset], _SourceState]] = []
        for name, assets in groups.items():
            state = self._states.setdefault(name, _SourceState())
            signature = (cfg.base_currency, tuple(assets))
            if signature != state.signature:
                # 銘柄や基準通貨が変わったら、間隔やバックオフに関係なく取り直す。
                keys = {a.key for a in assets}
                state.readings = {k: r for k, r in state.readings.items() if k in keys}
                state.errors = {}
                state.signature = signature
                state.failures = 0
                state.next_due = 0.0
            if now >= state.next_due:
                due.append((name, assets, state))

        results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_one, name, assets, cfg) for name, assets, _ in due),
            return_exceptions=True,
        )

        fresh: set[str] = set()
        for (name, assets, state), result in zip(due, results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                self._backoff(name, state, cfg, now, result)
                state.errors = {a.key: str(result) for a in assets}
                continue
            state.failures = 0
            self._schedule(name, state, cfg, now, result)
            for r in result.readings:
                state.readings[r.key] = r
                fresh.add(r.key)
            state.errors = dict(result.errors)
            for key, message in result.errors.items():
                log.warning("%s: %s", name, message)

        readings: list[Reading] = []
        errors: dict[str, str] = {}
        source_of = {a.key: name for name, assets in groups.items() for a in assets}
        for a in cfg.assets:
            state = self._states[source_of[a.key]]
            if a.key in state.readings:
                readings.append(state.readings[a.key])
            if a.key in state.errors:
                errors[a.key] = state.errors[a.key]
        return FetchOutcome(readings=readings, fresh=fresh, errors=errors)
