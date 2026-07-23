"""バックグラウンドのポーリングループ。

一定間隔で価格を取得し、ライブ状態を更新して SSE 購読者へ配信、
必要に応じて Discord へアラート／定期レポートを送る。
設定変更や「今すぐ更新」は wake イベントで待機を中断して即反映する。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..config import HISTORY_FILE, PRICES_FILE
from ..notify import build_alert_embed, build_report_embed, find_alerts, send
from ..providers import fetch_all
from ..storage import load_json, save_json
from .context import AppContext

log = logging.getLogger("poteto-monitor.poller")


async def _sleep_or_wake(ctx: AppContext, seconds: float) -> None:
    """指定秒だけ待つが、wake が set されたら即座に返る。"""
    try:
        await asyncio.wait_for(ctx.wake.wait(), timeout=max(1.0, seconds))
    except asyncio.TimeoutError:
        pass
    ctx.wake.clear()


async def _tick(ctx: AppContext, last_report_at: datetime | None) -> datetime | None:
    cfg = ctx.config
    readings = await asyncio.to_thread(fetch_all, cfg.assets, cfg.base_currency)
    now = datetime.now(timezone.utc)

    # 前回ポーリング比でアラート判定（リアルタイムの急変検知）。
    alerts = find_alerts(readings, ctx.state.previous)

    embeds = []
    now_str = now.strftime("%Y-%m-%d %H:%M")
    send_report = cfg.report_interval > 0 and (
        last_report_at is None or (now - last_report_at).total_seconds() >= cfg.report_interval
    )
    if send_report:
        embeds.append(build_report_embed(readings, ctx.state.previous, now_str))
    if alerts:
        embeds.append(build_alert_embed(alerts, now_str))
        log.warning("アラート %d 件: %s", len(alerts), [r.label for r, _ in alerts])

    if embeds and cfg.webhook_url:
        try:
            await asyncio.to_thread(send, cfg.webhook_url, embeds)
        except Exception as exc:  # noqa: BLE001 - 通知失敗で監視自体は止めない
            log.error("Discord 送信に失敗: %s", exc)

    # ライブ状態を更新して配信。
    ctx.state.update(readings, now.isoformat())
    ctx.state.broadcast()

    # 永続化: prices.json は毎回、history.json はレポート時のみ追記。
    await asyncio.to_thread(
        save_json, PRICES_FILE, {"last_updated": now.isoformat(), "values": {r.key: r.value for r in readings}}
    )
    if send_report:
        history = load_json(HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.append(
            {"timestamp": now.isoformat(), "values": {r.key: (r.fields or {"value": r.value}) for r in readings}}
        )
        await asyncio.to_thread(save_json, HISTORY_FILE, history[-cfg.history_limit :])

    # 次回比較のため previous を更新。
    ctx.state.previous = {r.key: r.value for r in readings}
    return now if send_report else last_report_at


async def poll_loop(ctx: AppContext) -> None:
    log.info("ポーラー開始（間隔 %ds）", ctx.config.poll_interval)
    last_report_at: datetime | None = None
    while not ctx.stop.is_set():
        try:
            last_report_at = await _tick(ctx, last_report_at)
        except Exception as exc:  # noqa: BLE001 - 一時的な取得失敗で落とさない
            log.error("取得に失敗: %s", exc)
            ctx.state.record_error(str(exc))
            ctx.state.broadcast()
        await _sleep_or_wake(ctx, ctx.config.poll_interval)
    log.info("ポーラー停止")
