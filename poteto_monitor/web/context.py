"""ポーラーと Web サーバーで共有するアプリケーションコンテキスト。"""

from __future__ import annotations

import asyncio
import time

from .. import config as config_mod
from ..config import Config, load_config
from ..history import HistoryStore
from .auth import FailureLimiter
from .fetcher import SourceScheduler
from .state import LiveState

MIN_REFRESH_INTERVAL = 10  # 秒。「今すぐ更新」の連打で上流 API を叩かせないための下限


class AppContext:
    def __init__(self, cfg: Config, history: HistoryStore | None = None) -> None:
        self.config = cfg
        self.state = LiveState()
        self.state.set_meta(poll_interval=cfg.poll_interval, base_currency=cfg.base_currency)
        # ポーラーの待機を中断させるためのイベント（設定変更・即時更新で set）。
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self._last_refresh: float | None = None
        self.fetcher = SourceScheduler()
        self.history = history or HistoryStore(config_mod.HISTORY_DB)
        self.background: set[asyncio.Task] = set()  # 実行中の Discord 送信タスク（GC 防止）
        self.last_tick: float | None = None  # ポーラーが最後に 1 周した時刻（/healthz 用）
        self.last_prune: float = 0.0
        self.outage_notified: set[str] = set()  # 障害を通知済みのデータソース
        self.auth_limiter = FailureLimiter()

    async def close(self) -> None:
        """終了時に送信中の通知を待ち（上限付き）、HTTP 接続を閉じる。"""
        if self.background:
            await asyncio.wait(self.background, timeout=5)
        self.fetcher.close()

    def reload(self) -> Config:
        """config.json を再読込し、ポーラーを起こす。"""
        self.config = load_config()
        self.state.set_meta(
            poll_interval=self.config.poll_interval,
            base_currency=self.config.base_currency,
        )
        self.wake.set()
        return self.config

    def trigger_refresh(self) -> bool:
        """即時取得を要求する。前回の要求から MIN_REFRESH_INTERVAL 未満なら無視して False を返す。"""
        now = time.monotonic()
        if self._last_refresh is not None and now - self._last_refresh < MIN_REFRESH_INTERVAL:
            return False
        self._last_refresh = now
        self.wake.set()
        return True
