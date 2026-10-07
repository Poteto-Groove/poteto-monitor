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
        self.previous: dict[str, float] = {}
        self.changes: dict[str, float | None] = {}  # 直前の取得からの変化率（update 時に確定）
        self._previous_base: str | None = None  # previous を取得したときの基準通貨
        self._base_dependent: set[str] = set()  # 基準通貨で値の単位が変わるキー
        self.updated_at: str | None = None
        self.status: str = "starting"  # starting | ok | error
        self.error: str | None = None
        self.poll_interval: int = 0
        self.base_currency: str = "usd"
        self._subscribers: set[asyncio.Queue] = set()

    # ── 更新 ─────────────────────────────────────────────────────────
    def set_meta(self, *, poll_interval: int, base_currency: str) -> None:
        self.poll_interval = poll_interval
        self.base_currency = base_currency

    def comparable_previous(self, base_currency: str) -> dict[str, float]:
        """base_currency の値と比べてよい前回値（基準通貨が変わった crypto は除く）。"""
        if self._previous_base in (None, base_currency):
            return self.previous
        return {k: v for k, v in self.previous.items() if k not in self._base_dependent}

    def update(self, readings: list[Reading], updated_at: str, base_currency: str) -> None:
        previous = self.comparable_previous(base_currency)
        self.changes = {r.key: pct_change(previous.get(r.key, 0.0), r.value) for r in readings}
        self.previous = {r.key: r.value for r in readings}
        self._previous_base = base_currency
        self._base_dependent = {r.key for r in readings if r.type == "crypto"}
        self.readings = readings
        self.updated_at = updated_at
        self.status = "ok"
        self.error = None

    def record_error(self, message: str) -> None:
        self.status = "error"
        self.error = message

    # ── スナップショット ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        assets = []
        for r in self.readings:
            change = self.changes.get(r.key)
            assets.append(
                {
                    "key": r.key,
                    "label": r.label,
                    "emoji": r.emoji,
                    "type": r.type,
                    "display": r.display,
                    "value": r.value,
                    "change_pct": change,
                    "threshold": r.threshold,
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
