"""
poteto-monitor: BTC/ETH 価格監視スクリプト

動作:
  - CoinGecko API から BTC/ETH の USD/JPY 価格を取得
  - /var/lib/poteto-monitor/prices.json に前回価格を保存
  - 毎時: Discord Webhook へ定期レポートを送信
  - 10% 以上の変動時: アラート通知を追加送信
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── 設定 ──────────────────────────────────────────────────
COINGECKO_API = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum&vs_currencies=usd,jpy"
)
DATA_DIR     = Path(os.environ.get("POTETO_DATA_DIR", "/var/lib/poteto-monitor"))
PRICES_FILE  = DATA_DIR / "prices.json"
HISTORY_FILE = DATA_DIR / "history.json"
CONFIG_FILE  = DATA_DIR / "config.json"

ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "10"))
COIN_LABELS  = {"bitcoin": "Bitcoin (BTC)", "ethereum": "Ethereum (ETH)"}
COIN_EMOJIS  = {"bitcoin": "🟡", "ethereum": "🔷"}

# Discord embed カラー
COLOR_REPORT = 3447003   # 青
COLOR_UP     = 3066993   # 緑
COLOR_DOWN   = 15158332  # 赤
COLOR_ALERT  = 15844367  # オレンジ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
# ──────────────────────────────────────────────────────────


def load_config() -> dict:
    """config.json から設定を読み込む（環境変数で上書き可能）"""
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    return {
        "webhook_url": os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("webhook_url", ""),
        "alert_threshold": float(os.environ.get("ALERT_THRESHOLD") or cfg.get("alert_threshold", 10)),
    }


def fetch_prices() -> dict:
    resp = requests.get(COINGECKO_API, timeout=15)
    resp.raise_for_status()
    return resp.json()


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def pct_change(old: float, new: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100


def fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "初回取得"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def trend_emoji(pct: float | None) -> str:
    if pct is None:
        return "🆕"
    if pct >= 5:   return "🚀"
    if pct > 0:    return "📈"
    if pct <= -5:  return "💥"
    if pct < 0:    return "📉"
    return "➡️"


def build_report_embed(prices: dict, prev_prices: dict, now_str: str, threshold: float) -> dict:
    """毎時定期レポート用 embed"""
    fields = []
    overall_up = True

    for coin, label in COIN_LABELS.items():
        usd = prices[coin]["usd"]
        jpy = prices[coin]["jpy"]
        old_usd = prev_prices.get(coin, {}).get("usd", 0)
        pct = pct_change(old_usd, usd)
        if pct is not None and pct < 0:
            overall_up = False

        fields.append({
            "name": f"{COIN_EMOJIS[coin]} {label}",
            "value": (
                f"**${usd:,.2f}**  (¥{jpy:,.0f})\n"
                f"{trend_emoji(pct)}  前時比: **{fmt_pct(pct)}**"
            ),
            "inline": True,
        })

    color = COLOR_UP if overall_up else COLOR_DOWN

    return {
        "title": "📊 暗号資産 時間レポート",
        "color": color,
        "fields": fields,
        "footer": {"text": f"poteto-monitor  •  {now_str} UTC  •  アラート閾値 {threshold:.0f}%"},
    }


def build_alert_embed(alerts: list[tuple[str, float]], now_str: str) -> dict:
    """10%以上の変動アラート embed"""
    lines = []
    for label, pct in alerts:
        emoji = "🚀" if pct > 0 else "💥"
        lines.append(f"{emoji} **{label}**: {fmt_pct(pct)}")

    return {
        "title": "🚨 大きな価格変動を検知しました！",
        "description": "\n".join(lines),
        "color": COLOR_ALERT,
        "footer": {"text": f"poteto-monitor  •  {now_str} UTC"},
    }


def send_discord(webhook_url: str, embeds: list[dict]) -> None:
    resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=15)
    resp.raise_for_status()


def main() -> None:
    cfg = load_config()
    webhook_url = cfg["webhook_url"]
    threshold   = cfg["alert_threshold"]

    if not webhook_url:
        log.error("Discord Webhook URL が未設定です。")
        log.error("  config.json の webhook_url か環境変数 DISCORD_WEBHOOK_URL を設定してください。")
        sys.exit(1)

    log.info("価格を取得中...")
    prices = fetch_prices()
    now    = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    prev_data   = load_json(PRICES_FILE, {})
    prev_prices = prev_data.get("prices", {})

    # アラート対象を抽出
    alerts = []
    for coin, label in COIN_LABELS.items():
        old_usd = prev_prices.get(coin, {}).get("usd", 0)
        pct = pct_change(old_usd, prices[coin]["usd"])
        if pct is not None and abs(pct) >= threshold:
            alerts.append((label, pct))

    # Discord に通知（定期レポート + あればアラート）
    embeds = [build_report_embed(prices, prev_prices, now_str, threshold)]
    if alerts:
        embeds.append(build_alert_embed(alerts, now_str))
        log.warning("アラート発動: %s", alerts)

    send_discord(webhook_url, embeds)
    log.info("Discord に通知しました (%s UTC)", now_str)

    # prices.json 更新
    save_json(PRICES_FILE, {
        "last_updated": now.isoformat(),
        "prices": {
            "bitcoin":  prices["bitcoin"],
            "ethereum": prices["ethereum"],
        },
    })

    # history.json に追記（最大 168 件 = 7日分）
    history: list = load_json(HISTORY_FILE, [])
    history.append({
        "timestamp":    now.isoformat(),
        "bitcoin_usd":  prices["bitcoin"]["usd"],
        "bitcoin_jpy":  prices["bitcoin"]["jpy"],
        "ethereum_usd": prices["ethereum"]["usd"],
        "ethereum_jpy": prices["ethereum"]["jpy"],
    })
    save_json(HISTORY_FILE, history[-168:])

    log.info("BTC: $%s  ETH: $%s",
             f"{prices['bitcoin']['usd']:,.2f}",
             f"{prices['ethereum']['usd']:,.2f}")


if __name__ == "__main__":
    main()
