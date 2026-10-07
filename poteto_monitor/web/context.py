"""ポーラーと Web サーバーで共有するアプリケーションコンテキスト。"""

from __future__ import annotations

import asyncio
import time

from ..config import Config, load_config
from .state import LiveState

MIN_REFRESH_INTERVAL = 10  # 秒。「今すぐ更新」の連打で上流 API を叩かせないための下限


class AppContext:
    def __init__(self, cfg: Config) -> None:
        self.config = cfg
        self.state = LiveState()
        self.state.set_meta(poll_interval=cfg.poll_interval, base_currency=cfg.base_currency)
        # ポーラーの待機を中断させるためのイベント（設定変更・即時更新で set）。
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self._last_refresh: float | None = None

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
