"""通貨表示のための整形ヘルパー。"""

from __future__ import annotations

CURRENCY_SYMBOLS = {
    "usd": "$",
    "jpy": "¥",
    "eur": "€",
    "gbp": "£",
    "cny": "¥",
    "krw": "₩",
    "aud": "A$",
    "cad": "C$",
    "chf": "Fr",
    "btc": "₿",
}

# 小数を出さない（＝最小単位が 1 の）通貨。
ZERO_DECIMAL = {"jpy", "krw"}


def money(value: float, currency: str) -> str:
    """金額を通貨記号付きで整形する。"""
    code = currency.lower()
    symbol = CURRENCY_SYMBOLS.get(code, "")

    if code in ZERO_DECIMAL:
        body = f"{value:,.0f}"
    elif abs(value) != 0 and abs(value) < 1:
        # 端数の細かい暗号資産などは有効桁を確保する。
        body = f"{value:,.6f}".rstrip("0").rstrip(".")
    else:
        body = f"{value:,.2f}"

    if symbol:
        return f"{symbol}{body}"
    return f"{body} {currency.upper()}"


def rate(value: float, currency: str) -> str:
    """為替レートの整形。価格と違い、レートは JPY でも小数を残す。"""
    symbol = CURRENCY_SYMBOLS.get(currency.lower(), "")
    if abs(value) >= 100:
        body = f"{value:,.2f}"
    elif abs(value) >= 1:
        body = f"{value:,.4f}"
    else:
        body = f"{value:,.6f}"
    return f"{symbol}{body}" if symbol else f"{body} {currency.upper()}"


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
    if pct >= 5:
        return "🚀"
    if pct > 0:
        return "📈"
    if pct <= -5:
        return "💥"
    if pct < 0:
        return "📉"
    return "➡️"
