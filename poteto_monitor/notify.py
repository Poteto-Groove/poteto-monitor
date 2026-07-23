"""Discord Webhook への通知（embed 生成 + 送信）。"""

from __future__ import annotations

import requests

from .format import fmt_pct, pct_change, trend_emoji
from .models import Reading

TIMEOUT = 15

# Discord embed カラー
COLOR_UP = 3066993  # 緑
COLOR_DOWN = 15158332  # 赤
COLOR_ALERT = 15844367  # オレンジ


def build_report_embed(readings: list[Reading], previous: dict[str, float], now_str: str) -> dict:
    """定期レポート用 embed。"""
    fields = []
    overall_up = True

    for r in readings:
        old = previous.get(r.key, 0.0)
        pct = pct_change(old, r.value)
        if pct is not None and pct < 0:
            overall_up = False
        fields.append(
            {
                "name": f"{r.emoji} {r.label}",
                "value": f"**{r.display}**\n{trend_emoji(pct)}  前回比: **{fmt_pct(pct)}**",
                "inline": True,
            }
        )

    return {
        "title": "📊 マーケット定期レポート",
        "color": COLOR_UP if overall_up else COLOR_DOWN,
        "fields": fields,
        "footer": {"text": f"poteto-monitor  •  {now_str} UTC"},
    }


def build_alert_embed(alerts: list[tuple[Reading, float]], now_str: str) -> dict:
    """閾値超えの急変アラート embed。"""
    lines = []
    for r, pct in alerts:
        emoji = "🚀" if pct > 0 else "💥"
        lines.append(f"{emoji} **{r.label}**: {fmt_pct(pct)}  →  {r.display}")

    return {
        "title": "🚨 大きな変動を検知しました！",
        "description": "\n".join(lines),
        "color": COLOR_ALERT,
        "footer": {"text": f"poteto-monitor  •  {now_str} UTC"},
    }


def find_alerts(readings: list[Reading], previous: dict[str, float]) -> list[tuple[Reading, float]]:
    """アセットごとの閾値で急変を抽出する。"""
    alerts: list[tuple[Reading, float]] = []
    for r in readings:
        pct = pct_change(previous.get(r.key, 0.0), r.value)
        if pct is not None and abs(pct) >= r.threshold:
            alerts.append((r, pct))
    return alerts


def send(webhook_url: str, embeds: list[dict], *, session: requests.Session | None = None) -> None:
    http = session or requests
    resp = http.post(webhook_url, json={"embeds": embeds}, timeout=TIMEOUT)
    resp.raise_for_status()
