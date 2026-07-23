"""ポーラーと Web サーバーで共有するアプリケーションコンテキスト。"""

from __future__ import annotations

import asyncio

from ..config import Config, load_config
from .state import LiveState


class AppContext:
    def __init__(self, cfg: Config) -> None:
        self.config = cfg
        self.state = LiveState()
        self.state.set_meta(poll_interval=cfg.poll_interval, base_currency=cfg.base_currency)
        # ポーラーの待機を中断させるためのイベント（設定変更・即時更新で set）。
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()

    def reload(self) -> Config:
        """config.json を再読込し、ポーラーを起こす。"""
        self.config = load_config()
        self.state.set_meta(
            poll_interval=self.config.poll_interval,
            base_currency=self.config.base_currency,
        )
        self.wake.set()
        return self.config

    def trigger_refresh(self) -> None:
        self.wake.set()
