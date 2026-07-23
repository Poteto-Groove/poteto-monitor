"""価格プロバイダ。

- crypto: CoinGecko simple/price（1 リクエストで全銘柄）
- forex : open.er-api.com（基準通貨ごとに 1 リクエスト、API キー不要）

いずれもネットワークアクセスをこのモジュールに閉じ込め、テストしやすくしている。
"""

from __future__ import annotations

import requests

from .format import money, rate as fmt_rate
from .models import Asset, Reading

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
FOREX_URL = "https://open.er-api.com/v6/latest/{base}"
TIMEOUT = 15


class ProviderError(RuntimeError):
    """外部データ取得に失敗したときに送出。"""


def fetch_crypto(assets: list[Asset], base_currency: str, *, session: requests.Session | None = None) -> list[Reading]:
    """CoinGecko からまとめて価格を取得する。"""
    if not assets:
        return []

    coin_ids = sorted({a.coin_id for a in assets})
    vs_currencies = sorted({base_currency} | {c for a in assets for c in a.vs})
    params = {"ids": ",".join(coin_ids), "vs_currencies": ",".join(vs_currencies)}

    http = session or requests
    resp = http.get(COINGECKO_URL, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

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
                key=asset.key,
                label=asset.label,
                emoji=asset.emoji,
                value=value,
                display=display,
                threshold=asset.threshold,
                type="crypto",
                fields={c: float(quote[c]) for c in asset.vs if c in quote},
            )
        )
    return readings


def fetch_forex(assets: list[Asset], *, session: requests.Session | None = None) -> list[Reading]:
    """open.er-api.com から為替レートを取得する（基準通貨ごとにまとめる）。"""
    if not assets:
        return []

    http = session or requests
    bases = sorted({a.base for a in assets})
    rates_by_base: dict[str, dict[str, float]] = {}
    for base in bases:
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
        rate = float(rates[asset.quote])
        display = f"{fmt_rate(rate, asset.quote)}  / 1 {asset.base}"
        readings.append(
            Reading(
                key=asset.key,
                label=asset.label,
                emoji=asset.emoji,
                value=rate,
                display=display,
                threshold=asset.threshold,
                type="forex",
                fields={f"{asset.base}{asset.quote}": rate},
            )
        )
    return readings


def fetch_all(assets: list[Asset], base_currency: str, *, session: requests.Session | None = None) -> list[Reading]:
    """設定順を保ったまま全アセットの Reading を返す。"""
    crypto = [a for a in assets if a.type == "crypto"]
    forex = [a for a in assets if a.type == "forex"]

    by_key: dict[str, Reading] = {}
    for reading in fetch_crypto(crypto, base_currency, session=session):
        by_key[reading.key] = reading
    for reading in fetch_forex(forex, session=session):
        by_key[reading.key] = reading

    return [by_key[a.key] for a in assets if a.key in by_key]
