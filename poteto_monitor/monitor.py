"""オーケストレーション: 取得 → 通知 → 状態保存。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import __version__
from .config import HISTORY_FILE, PRICES_FILE, Config, ConfigError, load_config  # noqa: F401
from .notify import build_alert_embed, build_report_embed, find_alerts, send
from .providers import ProviderError, fetch_all
from .storage import load_json, load_previous, save_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poteto-monitor")


def run(cfg: Config, *, dry_run: bool = False) -> int:
    log.info("監視対象 %d 件を取得中...", len(cfg.assets))
    readings = fetch_all(cfg.assets, cfg.base_currency)

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    previous = load_previous(PRICES_FILE)

    alerts = find_alerts(readings, previous)
    embeds = [build_report_embed(readings, previous, now_str)]
    if alerts:
        embeds.append(build_alert_embed(alerts, now_str))
        log.warning("アラート %d 件: %s", len(alerts), [r.label for r, _ in alerts])

    for r in readings:
        log.info("  %s %s = %s", r.emoji, r.label, r.display)

    if dry_run:
        log.info("--dry-run: Discord 送信と状態保存をスキップしました")
        return 0

    send(cfg.webhook_url, embeds)
    log.info("Discord に通知しました (%s UTC)", now_str)

    # 前回価格を更新（次回の変化率計算用）。
    save_json(
        PRICES_FILE,
        {"last_updated": now.isoformat(), "values": {r.key: r.value for r in readings}},
    )

    # 履歴を追記（末尾 history_limit 件を保持）。
    history = load_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []
    history.append(
        {"timestamp": now.isoformat(), "values": {r.key: r.fields or {"value": r.value} for r in readings}}
    )
    save_json(HISTORY_FILE, history[-cfg.history_limit :])
    return 0


def serve(cfg: Config) -> int:
    """Web ダッシュボード + 常駐ポーラーを起動する。"""
    try:
        import uvicorn

        from .web.context import AppContext
        from .web.server import create_app
    except ImportError:
        log.error("Web 機能には追加依存が必要です: pip install 'poteto-monitor[web]'")
        return 3

    ctx = AppContext(cfg)
    app = create_app(ctx)
    log.info("Web ダッシュボードを起動: http://%s:%d", cfg.web_host, cfg.web_port)
    if not cfg.web_auth_token:
        log.warning("web.auth_token 未設定です。公開する場合は Cloudflare Access 等で保護してください。")
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poteto-monitor",
        description="暗号資産と為替レートを監視して Discord へ通知します。",
    )
    parser.add_argument("command", nargs="?", default="run", choices=["run", "serve"],
                        help="run: 1 回実行（既定） / serve: Web ダッシュボードを常駐起動")
    parser.add_argument("--dry-run", action="store_true", help="取得・整形のみ行い、送信も保存もしない")
    parser.add_argument("--version", action="version", version=f"poteto-monitor {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("設定エラー: %s", exc)
        return 2

    if args.command == "serve":
        return serve(cfg)

    if not args.dry_run and not cfg.webhook_url:
        log.error("Discord Webhook URL が未設定です。")
        log.error("  config.json の webhook_url か環境変数 DISCORD_WEBHOOK_URL を設定してください。")
        return 1

    try:
        return run(cfg, dry_run=args.dry_run)
    except (ProviderError, Exception) as exc:  # noqa: BLE001 - 単発実行なので握ってログに残す
        log.error("実行に失敗しました: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
