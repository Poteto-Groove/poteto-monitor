"""バックグラウンドのポーリングループ。

一定間隔で価格を取得し、ライブ状態を更新して SSE 購読者へ配信、
必要に応じて Discord へアラート／定期レポート／データソース障害を送る。
設定変更や「今すぐ更新」は wake イベントで待機を中断して即反映する。
各データソースを実際に取りに行くかどうかは SourceScheduler が取得間隔で判断する。

新しく取得できた値は履歴 DB に記録し、急変アラート（alert_window 秒前比）・
24 時間変化率・定期レポートの前回比はその履歴と比べる。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..config import PRICES_FILE, Config
from ..format import pct_change
from ..history import sample_base
from ..models import Reading
from ..notify import build_alert_embed, build_report_embed, build_source_embed, find_alerts, send
from ..storage import load_json, save_json
from .context import AppContext

log = logging.getLogger("poteto-monitor.poller")

OUTAGE_NOTIFY_AFTER = 600  # 秒。データソースの失敗がこれだけ続いたら Discord に通知する
PRUNE_INTERVAL = 3600  # 秒。履歴 DB の古い行を消す間隔
DAY = 86400


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
    """前回保存した値を読み込み、再起動をまたいで変化率の表示を続ける。"""
    data = load_json(PRICES_FILE, {})
    values = data.get("values") if isinstance(data, dict) else None
    if isinstance(values, dict):
        base = str(data.get("base_currency") or ctx.config.base_currency)
        ctx.state.restore({k: float(v) for k, v in values.items() if isinstance(v, (int, float))}, base)


def _window_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 時間"
    return f"{seconds // 60} 分"


def _pairs(readings: list[Reading], cfg: Config) -> list[tuple[str, str]]:
    return [(r.key, sample_base(r.type, cfg.base_currency)) for r in readings]


def _window_alerts(ctx: AppContext, fresh: list[Reading], ts: int) -> list[tuple[Reading, float]]:
    """alert_window 秒前の値と比べて閾値を超え、クールダウン中でない銘柄を返す（同期: スレッドで実行）。"""
    cfg, store = ctx.config, ctx.history
    # 窓の 1/4（最大 1 時間）以内に記録が無ければ比較しない（停止明けの誤検知を防ぐ）。
    refs = store.values_at(_pairs(fresh, cfg), ts - cfg.alert_window, min(cfg.alert_window // 4, 3600))
    alerts = []
    for reading, pct in find_alerts(fresh, refs):
        last = store.get_meta(f"alert:{reading.key}")
        if last is not None and ts - int(last) < cfg.alert_cooldown:
            continue
        store.set_meta(f"alert:{reading.key}", str(ts))
        alerts.append((reading, pct))
    return alerts


def _report_due(ctx: AppContext, readings: list[Reading], ts: int) -> dict[str, float] | None:
    """定期レポートを送るなら前回レポート時点の値を返す（同期: スレッドで実行）。"""
    cfg, store = ctx.config, ctx.history
    if cfg.report_interval <= 0 or not readings:
        return None
    last = store.get_meta("last_report_at")
    if last is not None and ts - int(last) < cfg.report_interval:
        return None
    ref_ts = int(last) if last is not None else ts - cfg.report_interval
    store.set_meta("last_report_at", str(ts))
    return store.values_at(_pairs(readings, cfg), ref_ts, cfg.report_interval)


def _source_events(ctx: AppContext, now: float) -> tuple[dict[str, str], list[str]]:
    """OUTAGE_NOTIFY_AFTER 以上続く障害と、通知済み障害の復旧を拾う。"""
    failing: dict[str, str] = {}
    recovered: list[str] = []
    for name, h in ctx.fetcher.health().items():
        if not h.ok and h.failing_since is not None and now - h.failing_since >= OUTAGE_NOTIFY_AFTER:
            if name not in ctx.outage_notified:
                failing[name] = h.error or "不明なエラー"
                ctx.outage_notified.add(name)
        elif h.ok and name in ctx.outage_notified:
            recovered.append(name)
            ctx.outage_notified.discard(name)
    return failing, recovered


def _record(ctx: AppContext, fresh: list[Reading], readings: list[Reading], ts: int) -> dict[str, float | None]:
    """新しい値を履歴に記録し、全銘柄の 24 時間変化率を返す（同期: スレッドで実行）。"""
    cfg, store = ctx.config, ctx.history
    store.add((key, base, ts, r.value) for r, (key, base) in zip(fresh, _pairs(fresh, cfg)))
    refs = store.values_at(_pairs(readings, cfg), ts - DAY, 3600)
    if ts - ctx.last_prune >= PRUNE_INTERVAL:
        removed = store.prune(ts - cfg.retention_days * DAY)
        ctx.last_prune = ts
        if removed:
            log.info("履歴 DB から %d 行を削除（%d 日より前）", removed, cfg.retention_days)
    return {r.key: pct_change(refs.get(r.key, 0.0), r.value) for r in readings}


async def _tick(ctx: AppContext, now: datetime | None = None) -> None:
    cfg = ctx.config
    outcome = await ctx.fetcher.fetch(cfg)
    now = now or datetime.now(timezone.utc)
    ts = int(now.timestamp())
    fresh = [r for r in outcome.readings if r.key in outcome.fresh]

    changes_24h = await asyncio.to_thread(_record, ctx, fresh, outcome.readings, ts)
    alerts = await asyncio.to_thread(_window_alerts, ctx, fresh, ts)
    report_refs = await asyncio.to_thread(_report_due, ctx, outcome.readings, ts)
    failing, recovered = _source_events(ctx, now.timestamp())

    # ライブ状態を更新して配信（変化率の確定と previous の更新もここで行う）。
    ctx.state.update(
        outcome.readings, now.isoformat(), cfg.base_currency,
        fresh=outcome.fresh, errors=outcome.errors, changes_24h=changes_24h,
    )
    ctx.state.broadcast()

    embeds = []
    now_str = now.strftime("%Y-%m-%d %H:%M")
    if report_refs is not None:
        embeds.append(build_report_embed(outcome.readings, report_refs, now_str))
    if alerts:
        embeds.append(build_alert_embed(alerts, now_str, _window_label(cfg.alert_window)))
        log.warning("アラート %d 件: %s", len(alerts), [r.label for r, _ in alerts])
    if failing or recovered:
        embeds.append(build_source_embed(failing, recovered, now_str))
    if embeds and cfg.webhook_url:
        _send_in_background(ctx, cfg.webhook_url, embeds)

    # prices.json は再起動後の前回比表示のため、新しい値を得たときだけ保存する。
    if outcome.fresh:
        await asyncio.to_thread(
            save_json,
            PRICES_FILE,
            {"last_updated": now.isoformat(), "base_currency": cfg.base_currency, "values": dict(ctx.state.previous)},
        )


async def poll_loop(ctx: AppContext) -> None:
    log.info("ポーラー開始（間隔 %ds）", ctx.config.poll_interval)
    _restore_previous(ctx)
    while not ctx.stop.is_set():
        try:
            await _tick(ctx)
        except Exception as exc:  # noqa: BLE001 - 一時的な取得失敗で落とさない
            log.error("取得に失敗: %s", exc)
            ctx.state.record_error(str(exc))
            ctx.state.broadcast()
        ctx.last_tick = time.time()
        await _sleep_or_wake(ctx, ctx.config.poll_interval)
    log.info("ポーラー停止")
