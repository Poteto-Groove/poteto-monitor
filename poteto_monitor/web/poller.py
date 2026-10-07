"""バックグラウンドのポーリングループ。

一定間隔で価格を取得し、ライブ状態を更新して SSE 購読者へ配信、
必要に応じて Discord へアラート／定期レポートを送る。
設定変更や「今すぐ更新」は wake イベントで待機を中断して即反映する。
各データソースを実際に取りに行くかどうかは SourceScheduler が取得間隔で判断する。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..config import HISTORY_FILE, PRICES_FILE
from ..notify import build_alert_embed, build_report_embed, find_alerts, send
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


def _send_in_background(ctx: AppContext, webhook_url: str, embeds: list[dict]) -> None:
    """Discord 送信を別タスクで行い、画面への配信を待たせない。"""

    async def run() -> None:
        try:
            await asyncio.to_thread(send, webhook_url, embeds)
        except Exception as exc:  # noqa: BLE001 - 通知失敗で監視自体は止めない
            log.error("Discord 送信に失敗: %s", exc)

    task = asyncio.create_task(run())
    ctx.background.add(task)
    task.add_done_callback(ctx.background.discard)


def _restore_previous(ctx: AppContext) -> None:
    """前回保存した値を読み込み、再起動をまたいで変化率・アラート判定を続ける。"""
    data = load_json(PRICES_FILE, {})
    values = data.get("values") if isinstance(data, dict) else None
    if isinstance(values, dict):
        base = str(data.get("base_currency") or ctx.config.base_currency)
        ctx.state.restore({k: float(v) for k, v in values.items() if isinstance(v, (int, float))}, base)


async def _tick(ctx: AppContext, last_report_at: datetime | None) -> datetime | None:
    cfg = ctx.config
    outcome = await ctx.fetcher.fetch(cfg)
    now = datetime.now(timezone.utc)

    # 前回取得比でアラート判定（新しく取得できた銘柄のみ）。
    # 基準通貨が変わった銘柄は単位が違うので比較しない。
    previous = ctx.state.comparable_previous(cfg.base_currency)
    alerts = find_alerts([r for r in outcome.readings if r.key in outcome.fresh], previous)

    embeds = []
    now_str = now.strftime("%Y-%m-%d %H:%M")
    send_report = cfg.report_interval > 0 and bool(outcome.readings) and (
        last_report_at is None or (now - last_report_at).total_seconds() >= cfg.report_interval
    )
    if send_report:
        embeds.append(build_report_embed(outcome.readings, previous, now_str))
    if alerts:
        embeds.append(build_alert_embed(alerts, now_str))
        log.warning("アラート %d 件: %s", len(alerts), [r.label for r, _ in alerts])

    # ライブ状態を更新して配信（変化率の確定と previous の更新もここで行う）。
    ctx.state.update(
        outcome.readings, now.isoformat(), cfg.base_currency, fresh=outcome.fresh, errors=outcome.errors
    )
    ctx.state.broadcast()

    if embeds and cfg.webhook_url:
        _send_in_background(ctx, cfg.webhook_url, embeds)

    # 永続化: prices.json は新しい値を得たときだけ、history.json はレポート時のみ追記。
    if outcome.fresh:
        await asyncio.to_thread(
            save_json,
            PRICES_FILE,
            {"last_updated": now.isoformat(), "base_currency": cfg.base_currency, "values": dict(ctx.state.previous)},
        )
    if send_report:
        history = load_json(HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.append(
            {"timestamp": now.isoformat(), "values": {r.key: (r.fields or {"value": r.value}) for r in outcome.readings}}
        )
        await asyncio.to_thread(save_json, HISTORY_FILE, history[-cfg.history_limit :])

    return now if send_report else last_report_at


async def poll_loop(ctx: AppContext) -> None:
    log.info("ポーラー開始（間隔 %ds）", ctx.config.poll_interval)
    _restore_previous(ctx)
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
