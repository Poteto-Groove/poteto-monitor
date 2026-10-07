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


def build_alert_embed(alerts: list[tuple[Reading, float]], now_str: str, window: str = "") -> dict:
    """閾値超えの急変アラート embed。window は比較期間の表記（例: "1 時間"）。"""
    lines = [f"直近 {window} の変化" if window else ""]
    for r, pct in alerts:
        emoji = "🚀" if pct > 0 else "💥"
        lines.append(f"{emoji} **{r.label}**: {fmt_pct(pct)}  →  {r.display}")

    return {
        "title": "🚨 大きな変動を検知しました！",
        "description": "\n".join(line for line in lines if line),
        "color": COLOR_ALERT,
        "footer": {"text": f"poteto-monitor  •  {now_str} UTC"},
    }


def build_source_embed(failing: dict[str, str], recovered: list[str], now_str: str) -> dict:
    """データソースの障害／復旧通知 embed。failing は {ソース名: 理由}。"""
    lines = [f"⚠️ **{name}**: {reason}" for name, reason in failing.items()]
    lines += [f"✅ **{name}**: 復旧しました" for name in recovered]
    return {
        "title": "⚠️ データソース障害" if failing else "✅ データソース復旧",
        "description": "\n".join(lines),
        "color": COLOR_ALERT if failing else COLOR_UP,
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


# Discord の上限: embed あたりのフィールド 25 個、1 メッセージの embed 10 個・合計 6,000 文字。
MAX_FIELDS = 25
MAX_EMBEDS = 10
MAX_CHARS = 6000


def _embed_chars(embed: dict) -> int:
    return (
        len(embed.get("title", "")) + len(embed.get("description", ""))
        + len((embed.get("footer") or {}).get("text", ""))
        + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))
    )


def split_messages(embeds: list[dict]) -> list[list[dict]]:
    """embed を Discord の上限に収まるよう分割し、メッセージ単位にまとめる。"""
    parts: list[dict] = []
    for embed in embeds:
        fields = embed.get("fields", [])
        if len(fields) <= MAX_FIELDS:
            parts.append(embed)
            continue
        chunks = [fields[i : i + MAX_FIELDS] for i in range(0, len(fields), MAX_FIELDS)]
        for n, chunk in enumerate(chunks, 1):
            parts.append({**embed, "title": f"{embed.get('title', '')} ({n}/{len(chunks)})", "fields": chunk})

    messages: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for embed in parts:
        size = _embed_chars(embed)
        if current and (len(current) >= MAX_EMBEDS or chars + size > MAX_CHARS):
            messages.append(current)
            current, chars = [], 0
        current.append(embed)
        chars += size
    if current:
        messages.append(current)
    return messages


def send(webhook_url: str, embeds: list[dict], *, session: requests.Session | None = None) -> None:
    http = session or requests
    for message in split_messages(embeds):
        resp = http.post(webhook_url, json={"embeds": message}, timeout=TIMEOUT)
        resp.raise_for_status()
