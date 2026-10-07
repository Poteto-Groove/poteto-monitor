"""ライブ状態と SSE 向けの簡易 pub/sub。

ブラウザには「最新スナップショット + 更新のたびの push」を配信する。
スパークラインなどの時系列はブラウザ側でストリームを蓄積して描く。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..format import pct_change
from ..models import Reading


class LiveState:
    def __init__(self) -> None:
        self.readings: list[Reading] = []
        self.previous: dict[str, float] = {}  # 銘柄ごとの直近に取得できた値
        self.changes: dict[str, float | None] = {}  # 直前の取得からの変化率（update 時に確定）
        self.errors: dict[str, str] = {}  # 直近の取得に失敗している銘柄 -> 理由
        self._value_base: dict[str, str] = {}  # previous の値の単位になっている基準通貨（crypto のみ）
        self.updated_at: str | None = None
        self.status: str = "starting"  # starting | ok | degraded | error
        self.error: str | None = None
        self.poll_interval: int = 0
        self.base_currency: str = "usd"
        self._subscribers: set[asyncio.Queue] = set()

    # ── 更新 ─────────────────────────────────────────────────────────
    def set_meta(self, *, poll_interval: int, base_currency: str) -> None:
        self.poll_interval = poll_interval
        self.base_currency = base_currency

    def restore(self, values: dict[str, float], base_currency: str) -> None:
        """再起動前に保存した前回値を読み込む（基準通貨が分からない値は比較から外れるよう記録）。"""
        self.previous = dict(values)
        self._value_base = {k: base_currency for k in values}

    def comparable_previous(self, base_currency: str) -> dict[str, float]:
        """base_currency の値と比べてよい前回値（基準通貨が違う値は除く）。"""
        return {k: v for k, v in self.previous.items() if self._value_base.get(k, base_currency) == base_currency}

    def update(
        self,
        readings: list[Reading],
        updated_at: str,
        base_currency: str,
        *,
        fresh: set[str] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        """取得結果を反映する。fresh に無い銘柄は前回の値と変化率をそのまま保つ（None なら全件が新しい値）。"""
        previous = self.comparable_previous(base_currency)
        keys = {r.key for r in readings}
        for r in readings:
            if fresh is not None and r.key not in fresh:
                continue
            self.changes[r.key] = pct_change(previous.get(r.key, 0.0), r.value)
            self.previous[r.key] = r.value
            if r.type == "crypto":
                self._value_base[r.key] = base_currency
            else:
                self._value_base.pop(r.key, None)
        self.changes = {k: v for k, v in self.changes.items() if k in keys}
        self.readings = readings
        self.updated_at = updated_at
        self.errors = dict(errors or {})
        if not self.errors:
            self.status, self.error = "ok", None
        else:
            healthy = any(r.key not in self.errors for r in readings)
            self.status = "degraded" if healthy else "error"
            self.error = " / ".join(dict.fromkeys(self.errors.values()))

    def record_error(self, message: str) -> None:
        self.status = "error"
        self.error = message

    # ── スナップショット ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        assets = []
        for r in self.readings:
            assets.append(
                {
                    "key": r.key,
                    "label": r.label,
                    "emoji": r.emoji,
                    "type": r.type,
                    "display": r.display,
                    "value": r.value,
                    "change_pct": self.changes.get(r.key),
                    "threshold": r.threshold,
                    "as_of": r.as_of,
                    "stale": r.key in self.errors,
                }
            )
        return {
            "status": self.status,
            "error": self.error,
            "updated_at": self.updated_at,
            "poll_interval": self.poll_interval,
            "base_currency": self.base_currency,
            "assets": assets,
        }

    # ── pub/sub ──────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self) -> None:
        """現在のスナップショットを全購読者へ配信（詰まっている購読者はスキップ）。"""
        snap = self.snapshot()
        for q in list(self._subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass
