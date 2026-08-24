"""価格プロバイダ（プラグイン式レジストリ）。

種別 (``Asset.type``) ごとに取得関数を ``PROVIDERS`` に登録する。
新しいデータソース（例: 別の DEX / 別の為替 API）を足したいときは、
同じシグネチャの関数を書いて ``PROVIDERS`` に 1 行足すだけでよい。

現在の対応:
- crypto      : CoinGecko simple/price（1 リクエストで全銘柄）
- forex       : open.er-api.com（基準通貨ごとに 1 リクエスト）
- hyperliquid : api.hyperliquid.xyz/info allMids（HYPE や perp の板中値）
- ratio       : 2 銘柄の比（例: JPYC/USDC）。CoinGecko 価格から算出

いずれも API キー不要。ネットワークアクセスをこのモジュールに閉じ込めている。
"""

from __future__ import annotations

from typing import Callable

import requests

from .format import money, rate as fmt_rate
from .models import Asset, Reading

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
FOREX_URL = "https://open.er-api.com/v6/latest/{base}"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT = 15


class ProviderError(RuntimeError):
    """外部データ取得に失敗したときに送出。"""


# 各プロバイダの共通シグネチャ: (assets, base_currency, *, session) -> list[Reading]
Provider = Callable[..., "list[Reading]"]


# ── 共通ヘルパー ──────────────────────────────────────────────────────
def _coingecko_prices(ids, vs, session: requests.Session | None = None) -> dict:
    """CoinGecko simple/price をまとめて叩いて生 JSON を返す。"""
    http = session or requests
    params = {
        "ids": ",".join(sorted({i for i in ids if i})),
        "vs_currencies": ",".join(sorted({c for c in vs if c})),
    }
    resp = http.get(COINGECKO_URL, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ── crypto ────────────────────────────────────────────────────────────
def fetch_crypto(assets: list[Asset], base_currency: str, *, session: requests.Session | None = None) -> list[Reading]:
    if not assets:
        return []
    ids = {a.coin_id for a in assets}
    vs = {base_currency} | {c for a in assets for c in a.vs}
    data = _coingecko_prices(ids, vs, session)

    readings: list[Reading] = []
    for asset in assets:
        quote = data.get(asset.coin_id)
        if not quote or base_currency not in quote:
            raise ProviderError(f"CoinGecko に '{asset.coin_id}' の {base_currency.upper()} 価格がありません")

        value = float(quote[base_currency])
        parts = [money(float(quote[c]), c) for c in asset.vs if c in quote]
        display = parts[0] if parts else money(value, base_currency)
        if len(parts) > 1:
            display = f"{parts[0]}  ({' / '.join(parts[1:])})"

        readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=display, threshold=asset.threshold, type="crypto",
                fields={c: float(quote[c]) for c in asset.vs if c in quote},
            )
        )
    return readings


# ── forex ─────────────────────────────────────────────────────────────
def fetch_forex(assets: list[Asset], base_currency: str = "", *, session: requests.Session | None = None) -> list[Reading]:
    if not assets:
        return []
    http = session or requests
    rates_by_base: dict[str, dict[str, float]] = {}
    for base in sorted({a.base for a in assets}):
        resp = http.get(FOREX_URL.format(base=base), timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise ProviderError(f"為替 API がエラーを返しました (base={base}): {data.get('error-type', 'unknown')}")
        rates_by_base[base] = data.get("rates", {})

    readings: list[Reading] = []
    for asset in assets:
        rates = rates_by_base.get(asset.base, {})
        if asset.quote not in rates:
            raise ProviderError(f"為替レート {asset.base}/{asset.quote} が取得できません")
        value = float(rates[asset.quote])
        display = f"{fmt_rate(value, asset.quote)}  / 1 {asset.base}"
        readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=display, threshold=asset.threshold, type="forex",
                fields={f"{asset.base}{asset.quote}": value},
            )
        )
    return readings


# ── hyperliquid ───────────────────────────────────────────────────────
def fetch_hyperliquid(assets: list[Asset], base_currency: str = "", *, session: requests.Session | None = None) -> list[Reading]:
    """Hyperliquid の allMids（perp 板中値, USD 建て）から取得する。"""
    if not assets:
        return []
    http = session or requests
    resp = http.post(HYPERLIQUID_URL, json={"type": "allMids"}, timeout=TIMEOUT)
    resp.raise_for_status()
    mids = resp.json()
    if not isinstance(mids, dict):
        raise ProviderError("Hyperliquid の応答が不正です")

    readings: list[Reading] = []
    for asset in assets:
        sym = asset.coin
        if sym not in mids:
            raise ProviderError(f"Hyperliquid に '{sym}' の mid がありません")
        value = float(mids[sym])
        readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=f"{money(value, 'usd')}", threshold=asset.threshold, type="hyperliquid",
                fields={"usd": value},
            )
        )
    return readings


# ── ratio（2 銘柄の比）─────────────────────────────────────────────────
def fetch_ratio(assets: list[Asset], base_currency: str = "usd", *, session: requests.Session | None = None) -> list[Reading]:
    """num / den の比を CoinGecko 価格から算出する（例: JPYC/USDC）。"""
    if not assets:
        return []
    ids = {a.num for a in assets} | {a.den for a in assets}
    data = _coingecko_prices(ids, ["usd"], session)

    readings: list[Reading] = []
    for asset in assets:
        pn = data.get(asset.num, {}).get("usd")
        pd = data.get(asset.den, {}).get("usd")
        if pn is None or pd is None:
            raise ProviderError(f"レート {asset.num}/{asset.den} の価格が取得できません")
        if float(pd) == 0:
            raise ProviderError(f"レート {asset.num}/{asset.den} の分母が 0 です")
        value = float(pn) / float(pd)
        display = f"1 {asset.num.upper()} = {value:.6g} {asset.den.upper()}"
        readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=display, threshold=asset.threshold, type="ratio",
                fields={"ratio": value, f"{asset.num}_usd": float(pn), f"{asset.den}_usd": float(pd)},
            )
        )
    return readings


# ── レジストリ ────────────────────────────────────────────────────────
PROVIDERS: dict[str, Provider] = {
    "crypto": fetch_crypto,
    "forex": fetch_forex,
    "hyperliquid": fetch_hyperliquid,
    "ratio": fetch_ratio,
}

SUPPORTED_TYPES = tuple(PROVIDERS.keys())


def fetch_all(assets: list[Asset], base_currency: str, *, session: requests.Session | None = None) -> list[Reading]:
    """設定順を保ったまま全アセットの Reading を返す（種別ごとに一括取得）。"""
    groups: dict[str, list[Asset]] = {}
    for a in assets:
        groups.setdefault(a.type, []).append(a)

    by_key: dict[str, Reading] = {}
    for atype, group in groups.items():
        provider = PROVIDERS.get(atype)
        if provider is None:
            raise ProviderError(f"未対応の種別です: {atype}")
        for reading in provider(group, base_currency, session=session):
            by_key[reading.key] = reading

    return [by_key[a.key] for a in assets if a.key in by_key]
