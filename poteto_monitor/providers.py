"""価格プロバイダ（データソース単位のプラグイン式レジストリ）。

上流 API（データソース）ごとに取得関数を ``SOURCES`` に登録する。
1 つのソースは複数の種別 (``Asset.type``) を受け持てる（例: CoinGecko は crypto と ratio）。
新しいデータソース（例: 別の DEX / 別の為替 API）を足したいときは、
同じシグネチャの関数を書いて ``SOURCES`` に 1 行足すだけでよい。

現在の対応:
- coingecko   : simple/price（crypto と ratio をまとめて 1 リクエスト）
- forex       : open.er-api.com（基準通貨ごとに 1 リクエスト、1 日 1 回更新）
- hyperliquid : api.hyperliquid.xyz/info allMids（HYPE や perp の板中値）

取得関数は銘柄単位の失敗（ID の打ち間違いなど）を ``SourceResult.errors`` に入れて返し、
リクエスト自体の失敗だけを例外にする。ネットワークアクセスをこのモジュールに閉じ込めている。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

from .format import money, rate as fmt_rate
from .models import Asset, Reading

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
FOREX_URL = "https://open.er-api.com/v6/latest/{base}"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT = 15
FOREX_RATE_LIMIT_WAIT = 1200  # 秒。open.er-api.com は制限超過で 429 を 20 分返す


class ProviderError(RuntimeError):
    """外部データ取得に失敗したときに送出。"""


class RateLimitError(ProviderError):
    """上流 API がレート制限 (429) を返したときに送出。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class SourceResult:
    """1 データソースの取得結果。"""

    readings: list[Reading] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # 取得できなかった Asset.key -> 理由
    next_update: float | None = None  # 上流が次にデータを更新する時刻（UNIX 秒、分かる場合のみ）


# 各ソースの共通シグネチャ: (assets, base_currency, *, session, api_key) -> SourceResult
SourceFetcher = Callable[..., SourceResult]


@dataclass(frozen=True)
class Source:
    types: tuple[str, ...]  # このソースが受け持つ Asset.type
    fetch: SourceFetcher


# ── 共通ヘルパー ──────────────────────────────────────────────────────
def _check(resp: requests.Response, name: str, default_retry_after: float | None = None) -> None:
    """429 は RateLimitError（Retry-After 付き）に、それ以外の HTTP エラーは raise_for_status に任せる。"""
    if resp.status_code == 429:
        retry_after = default_retry_after
        header = resp.headers.get("Retry-After")
        if header and header.strip().isdigit():
            retry_after = float(header)
        raise RateLimitError(f"{name} のレート制限に達しました (429)", retry_after)
    resp.raise_for_status()


def _iso_from_unix(ts) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _coingecko_prices(ids, vs, session: requests.Session | None = None, api_key: str = "") -> dict:
    """CoinGecko simple/price をまとめて叩いて生 JSON を返す。"""
    http = session or requests
    params = {
        "ids": ",".join(sorted({i for i in ids if i})),
        "vs_currencies": ",".join(sorted({c for c in vs if c})),
        "include_last_updated_at": "true",
    }
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    resp = http.get(COINGECKO_URL, params=params, headers=headers, timeout=TIMEOUT)
    _check(resp, "CoinGecko")
    return resp.json()


# ── coingecko（crypto + ratio）───────────────────────────────────────
def _crypto_reading(asset: Asset, base_currency: str, quote: dict) -> Reading:
    value = float(quote[base_currency])
    parts = [money(float(quote[c]), c) for c in asset.vs if c in quote]
    display = parts[0] if parts else money(value, base_currency)
    if len(parts) > 1:
        display = f"{parts[0]}  ({' / '.join(parts[1:])})"
    return Reading(
        key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
        display=display, threshold=asset.threshold, type="crypto",
        fields={c: float(quote[c]) for c in asset.vs if c in quote},
        as_of=_iso_from_unix(quote.get("last_updated_at")),
    )


def _ratio_reading(asset: Asset, qn: dict, qd: dict) -> Reading:
    pn, pd = float(qn["usd"]), float(qd["usd"])
    value = pn / pd
    # 2 銘柄のうち古い方の時刻をデータ時刻とする。
    stamps = [q["last_updated_at"] for q in (qn, qd) if q.get("last_updated_at") is not None]
    return Reading(
        key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
        display=f"1 {asset.num.upper()} = {value:.6g} {asset.den.upper()}",
        threshold=asset.threshold, type="ratio",
        fields={"ratio": value, f"{asset.num}_usd": pn, f"{asset.den}_usd": pd},
        as_of=_iso_from_unix(min(stamps)) if stamps else None,
    )


def fetch_coingecko(
    assets: list[Asset], base_currency: str, *, session: requests.Session | None = None, api_key: str = ""
) -> SourceResult:
    """crypto と ratio（num / den の比, USD 建て価格から算出）を 1 回の simple/price で取得する。"""
    result = SourceResult()
    if not assets:
        return result
    crypto = [a for a in assets if a.type == "crypto"]
    ratio = [a for a in assets if a.type == "ratio"]
    ids = {a.coin_id for a in crypto} | {a.num for a in ratio} | {a.den for a in ratio}
    vs = {c for a in crypto for c in a.vs} | ({base_currency} if crypto else set()) | ({"usd"} if ratio else set())
    data = _coingecko_prices(ids, vs, session, api_key)

    for asset in assets:
        if asset.type == "crypto":
            quote = data.get(asset.coin_id)
            if not quote or base_currency not in quote:
                result.errors[asset.key] = f"CoinGecko に '{asset.coin_id}' の {base_currency.upper()} 価格がありません"
                continue
            result.readings.append(_crypto_reading(asset, base_currency, quote))
        else:
            qn, qd = data.get(asset.num) or {}, data.get(asset.den) or {}
            if qn.get("usd") is None or qd.get("usd") is None:
                result.errors[asset.key] = f"レート {asset.num}/{asset.den} の価格が取得できません"
            elif float(qd["usd"]) == 0:
                result.errors[asset.key] = f"レート {asset.num}/{asset.den} の分母が 0 です"
            else:
                result.readings.append(_ratio_reading(asset, qn, qd))
    return result


# ── forex ─────────────────────────────────────────────────────────────
def fetch_forex(
    assets: list[Asset], base_currency: str = "", *, session: requests.Session | None = None, api_key: str = ""
) -> SourceResult:
    result = SourceResult()
    if not assets:
        return result
    http = session or requests
    responses: dict[str, dict] = {}
    for base in sorted({a.base for a in assets}):
        resp = http.get(FOREX_URL.format(base=base), timeout=TIMEOUT)
        _check(resp, "為替 API", FOREX_RATE_LIMIT_WAIT)
        data = resp.json()
        if data.get("result") != "success":
            raise ProviderError(f"為替 API がエラーを返しました (base={base}): {data.get('error-type', 'unknown')}")
        responses[base] = data
        nxt = data.get("time_next_update_unix")
        if isinstance(nxt, (int, float)):
            result.next_update = nxt if result.next_update is None else min(result.next_update, nxt)

    for asset in assets:
        data = responses.get(asset.base, {})
        rates = data.get("rates", {})
        if asset.quote not in rates:
            result.errors[asset.key] = f"為替レート {asset.base}/{asset.quote} が取得できません"
            continue
        value = float(rates[asset.quote])
        result.readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=f"{fmt_rate(value, asset.quote)}  / 1 {asset.base}",
                threshold=asset.threshold, type="forex",
                fields={f"{asset.base}{asset.quote}": value},
                as_of=_iso_from_unix(data.get("time_last_update_unix")),
            )
        )
    return result


# ── hyperliquid ───────────────────────────────────────────────────────
def fetch_hyperliquid(
    assets: list[Asset], base_currency: str = "", *, session: requests.Session | None = None, api_key: str = ""
) -> SourceResult:
    """Hyperliquid の allMids（perp 板中値, USD 建て）から取得する。"""
    result = SourceResult()
    if not assets:
        return result
    http = session or requests
    resp = http.post(HYPERLIQUID_URL, json={"type": "allMids"}, timeout=TIMEOUT)
    _check(resp, "Hyperliquid")
    mids = resp.json()
    if not isinstance(mids, dict):
        raise ProviderError("Hyperliquid の応答が不正です")

    for asset in assets:
        sym = asset.coin
        if sym not in mids:
            result.errors[asset.key] = f"Hyperliquid に '{sym}' の mid がありません"
            continue
        value = float(mids[sym])
        result.readings.append(
            Reading(
                key=asset.key, label=asset.label, emoji=asset.emoji, value=value,
                display=f"{money(value, 'usd')}", threshold=asset.threshold, type="hyperliquid",
                fields={"usd": value},
            )
        )
    return result


# ── レジストリ ────────────────────────────────────────────────────────
SOURCES: dict[str, Source] = {
    "coingecko": Source(types=("crypto", "ratio"), fetch=fetch_coingecko),
    "forex": Source(types=("forex",), fetch=fetch_forex),
    "hyperliquid": Source(types=("hyperliquid",), fetch=fetch_hyperliquid),
}

SUPPORTED_TYPES = tuple(t for s in SOURCES.values() for t in s.types)


def group_by_source(assets: list[Asset], sources: dict[str, Source] | None = None) -> dict[str, list[Asset]]:
    """設定順を保ったままアセットをデータソースごとに振り分ける。"""
    sources = SOURCES if sources is None else sources
    by_type = {t: name for name, s in sources.items() for t in s.types}
    groups: dict[str, list[Asset]] = {}
    for a in assets:
        name = by_type.get(a.type)
        if name is None:
            raise ProviderError(f"未対応の種別です: {a.type}")
        groups.setdefault(name, []).append(a)
    return groups


def fetch_all(
    assets: list[Asset], base_currency: str, *, session: requests.Session | None = None, coingecko_api_key: str = ""
) -> list[Reading]:
    """設定順を保ったまま全アセットの Reading を返す（1 回実行モード用。1 件でも失敗すれば例外）。"""
    by_key: dict[str, Reading] = {}
    errors: list[str] = []
    for name, group in group_by_source(assets).items():
        api_key = coingecko_api_key if name == "coingecko" else ""
        result = SOURCES[name].fetch(group, base_currency, session=session, api_key=api_key)
        by_key.update((r.key, r) for r in result.readings)
        errors.extend(result.errors.values())
    if errors:
        raise ProviderError(" / ".join(errors))
    return [by_key[a.key] for a in assets if a.key in by_key]
