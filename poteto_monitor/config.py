"""設定の読み込みと検証。

優先順位: 環境変数 > config.json > 既定値。
config.json が無くても、既定の監視リスト（BTC / ETH / ドル円）で動作します。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Asset

DATA_DIR = Path(os.environ.get("POTETO_DATA_DIR", "/var/lib/poteto-monitor"))
CONFIG_FILE = DATA_DIR / "config.json"
PRICES_FILE = DATA_DIR / "prices.json"
HISTORY_FILE = DATA_DIR / "history.json"  # 旧形式（import-history の既定の取り込み元）
HISTORY_DB = DATA_DIR / "history.db"

DEFAULT_THRESHOLD = 10.0
DEFAULT_RETENTION_DAYS = 30  # 履歴 DB に残す日数
DEFAULT_ALERT_WINDOW = 3600  # 秒。急変アラートは「この秒数前の値」と比べる
MIN_ALERT_WINDOW = 60
DEFAULT_ALERT_COOLDOWN = 3600  # 秒。同じ銘柄のアラートを再送しない時間
DEFAULT_POLL_INTERVAL = 60  # 秒。Web ダッシュボードの更新間隔
MIN_POLL_INTERVAL = 5  # API のレート制限を守るための下限
DEFAULT_REPORT_INTERVAL = 3600  # 秒。Discord 定期レポートの間隔
# 秒。データソースごとの最短取得間隔（常駐モード）。0 なら毎回の poll で取得する。
# forex は上流が次回更新時刻を返すときはそれまで待つ（この値は下限として使う）。
DEFAULT_INTERVALS: dict[str, int] = {"coingecko": 300, "forex": 3600, "hyperliquid": 0}
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8787

# config.json が無い場合の既定監視リスト。
DEFAULT_WATCH: list[dict] = [
    {"type": "crypto", "id": "bitcoin", "label": "Bitcoin (BTC)", "emoji": "🟡", "vs": ["usd", "jpy"]},
    {"type": "crypto", "id": "ethereum", "label": "Ethereum (ETH)", "emoji": "🔷", "vs": ["usd", "jpy"]},
    {"type": "hyperliquid", "coin": "HYPE", "label": "HYPE (Hyperliquid)", "emoji": "⚡"},
    {"type": "forex", "base": "USD", "quote": "JPY", "label": "ドル円 (USD/JPY)", "emoji": "💴", "threshold": 2},
]


class ConfigError(ValueError):
    """設定が不正なときに送出。"""


@dataclass
class Config:
    webhook_url: str
    alert_threshold: float
    base_currency: str
    assets: list[Asset]
    poll_interval: int = DEFAULT_POLL_INTERVAL
    report_interval: int = DEFAULT_REPORT_INTERVAL
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    web_auth_token: str = ""
    web_protect_read: bool = True  # トークン設定時、閲覧（状態・履歴・SSE）にも認証を求めるか
    intervals: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_INTERVALS))
    coingecko_api_key: str = ""  # Demo キー。環境変数 COINGECKO_API_KEY からのみ読む
    retention_days: int = DEFAULT_RETENTION_DAYS
    alert_window: int = DEFAULT_ALERT_WINDOW
    alert_cooldown: int = DEFAULT_ALERT_COOLDOWN


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_float(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' は数値である必要があります: {value!r}") from None


def _to_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' は整数である必要があります: {value!r}") from None


def _parse_asset(raw: dict, index: int, default_threshold: float) -> Asset:
    if not isinstance(raw, dict):
        raise ConfigError(f"watch[{index}] はオブジェクトである必要があります")

    atype = str(raw.get("type", "")).strip().lower()
    threshold = _to_float(raw.get("threshold", default_threshold), f"watch[{index}].threshold")

    if atype == "crypto":
        coin_id = str(raw.get("id", "")).strip().lower()
        if not coin_id:
            raise ConfigError(f"watch[{index}] (crypto) には 'id' が必要です")
        raw_vs = raw.get("vs", ["usd", "jpy"])
        if isinstance(raw_vs, str):
            raw_vs = raw_vs.split(",")  # "usd" / "usd,jpy" 形式も許可
        elif not isinstance(raw_vs, list):
            raise ConfigError(f"watch[{index}] (crypto) の 'vs' はリストである必要があります")
        vs = tuple(str(c).strip().lower() for c in raw_vs if str(c).strip())
        if not vs:
            raise ConfigError(f"watch[{index}] (crypto) の 'vs' が空です")
        label = str(raw.get("label") or coin_id.upper())
        return Asset(
            type="crypto",
            key=raw.get("key") or f"crypto:{coin_id}",
            label=label,
            emoji=str(raw.get("emoji", "🪙")),
            threshold=threshold,
            coin_id=coin_id,
            vs=vs,
        )

    if atype == "forex":
        base = str(raw.get("base", "")).strip().upper()
        quote = str(raw.get("quote", "")).strip().upper()
        # "pair": "USD/JPY" 形式も許可。
        if not base and not quote and raw.get("pair"):
            parts = str(raw["pair"]).replace("-", "/").split("/")
            if len(parts) == 2:
                base, quote = parts[0].strip().upper(), parts[1].strip().upper()
        if not base or not quote:
            raise ConfigError(f"watch[{index}] (forex) には 'base' と 'quote' が必要です")
        label = str(raw.get("label") or f"{base}/{quote}")
        return Asset(
            type="forex",
            key=raw.get("key") or f"forex:{base}{quote}",
            label=label,
            emoji=str(raw.get("emoji", "💱")),
            threshold=threshold,
            base=base,
            quote=quote,
        )

    if atype == "hyperliquid":
        # kPEPE のように小文字を含む銘柄があるため大文字化しない（照合は providers 側で行う）。
        coin = str(raw.get("coin", "")).strip()
        if not coin:
            raise ConfigError(f"watch[{index}] (hyperliquid) には 'coin' が必要です（例: HYPE, BTC）")
        market = str(raw.get("market", "perp")).strip().lower()
        label = str(raw.get("label") or f"{coin} (Hyperliquid)")
        return Asset(
            type="hyperliquid",
            key=raw.get("key") or f"hl:{coin.upper()}",
            label=label,
            emoji=str(raw.get("emoji", "⚡")),
            threshold=threshold,
            coin=coin,
            market=market,
        )

    if atype == "ratio":
        num = str(raw.get("num", "")).strip().lower()
        den = str(raw.get("den", "")).strip().lower()
        # "pair": "jpyc/usd-coin" 形式も許可。
        if not num and not den and raw.get("pair"):
            parts = str(raw["pair"]).split("/")
            if len(parts) == 2:
                num, den = parts[0].strip().lower(), parts[1].strip().lower()
        if not num or not den:
            raise ConfigError(f"watch[{index}] (ratio) には 'num' と 'den'（CoinGecko ID）が必要です")
        label = str(raw.get("label") or f"{num.upper()}/{den.upper()}")
        return Asset(
            type="ratio",
            key=raw.get("key") or f"ratio:{num}/{den}",
            label=label,
            emoji=str(raw.get("emoji", "🪙")),
            threshold=threshold,
            num=num,
            den=den,
        )

    raise ConfigError(
        f"watch[{index}] の type '{atype}' は未対応です (crypto / forex / hyperliquid / ratio)"
    )


def parse_config(raw: dict) -> Config:
    """辞書から Config を組み立てる（環境変数の上書きも適用）。"""
    default_threshold = _to_float(
        os.environ.get("ALERT_THRESHOLD") or raw.get("alert_threshold", DEFAULT_THRESHOLD),
        "alert_threshold",
    )
    watch = raw.get("watch") or DEFAULT_WATCH
    if not isinstance(watch, list) or not watch:
        raise ConfigError("'watch' は空でないリストである必要があります")

    assets: list[Asset] = []
    seen: set[str] = set()
    for i, entry in enumerate(watch):
        asset = _parse_asset(entry, i, default_threshold)
        if asset.key in seen:
            raise ConfigError(f"重複したキー '{asset.key}' があります（key で区別してください）")
        seen.add(asset.key)
        assets.append(asset)

    web = raw.get("web") if isinstance(raw.get("web"), dict) else {}
    poll_interval = _to_int(
        os.environ.get("POLL_INTERVAL") or raw.get("poll_interval", DEFAULT_POLL_INTERVAL), "poll_interval"
    )
    report_interval = _to_int(raw.get("report_interval", DEFAULT_REPORT_INTERVAL), "report_interval")
    # 旧設定の history_limit は履歴 DB 移行で不要になったため読み飛ばす。
    retention_days = _to_int(raw.get("retention_days", DEFAULT_RETENTION_DAYS), "retention_days")
    if retention_days < 1:
        raise ConfigError("'retention_days' は 1 以上である必要があります")
    alert_window = _to_int(raw.get("alert_window", DEFAULT_ALERT_WINDOW), "alert_window")
    if alert_window < MIN_ALERT_WINDOW:
        raise ConfigError(f"'alert_window' は {MIN_ALERT_WINDOW} 秒以上である必要があります")
    alert_cooldown = _to_int(raw.get("alert_cooldown", DEFAULT_ALERT_COOLDOWN), "alert_cooldown")
    if alert_cooldown < 0:
        raise ConfigError("'alert_cooldown' は 0 以上である必要があります")

    raw_intervals = raw.get("intervals", {})
    if not isinstance(raw_intervals, dict):
        raise ConfigError("'intervals' はオブジェクトである必要があります")
    unknown = set(raw_intervals) - set(DEFAULT_INTERVALS)
    if unknown:
        raise ConfigError(f"'intervals' のキーが不明です: {', '.join(sorted(unknown))}")
    intervals = dict(DEFAULT_INTERVALS)
    for name, value in raw_intervals.items():
        intervals[name] = _to_int(value, f"intervals.{name}")
        if intervals[name] < 0:
            raise ConfigError(f"'intervals.{name}' は 0 以上である必要があります")

    return Config(
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or raw.get("webhook_url", ""),
        alert_threshold=default_threshold,
        base_currency=str(os.environ.get("BASE_CURRENCY") or raw.get("base_currency", "usd")).lower(),
        assets=assets,
        poll_interval=max(MIN_POLL_INTERVAL, poll_interval),
        report_interval=max(0, report_interval),
        web_host=str(os.environ.get("WEB_HOST") or web.get("host", DEFAULT_WEB_HOST)),
        web_port=_to_int(os.environ.get("WEB_PORT") or web.get("port", DEFAULT_WEB_PORT), "web.port"),
        web_auth_token=str(os.environ.get("WEB_AUTH_TOKEN") or web.get("auth_token", "")),
        web_protect_read=_as_bool(os.environ.get("WEB_PROTECT_READ") or web.get("protect_read", True)),
        intervals=intervals,
        coingecko_api_key=os.environ.get("COINGECKO_API_KEY", "").strip(),
        retention_days=retention_days,
        alert_window=alert_window,
        alert_cooldown=alert_cooldown,
    )


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_FILE
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} の JSON が不正です: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} はオブジェクトである必要があります")
    return parse_config(raw)


# ── Web エディタ用の生 JSON 入出力 ────────────────────────────────────
WEBHOOK_MASK = "__keep__"  # UI に生の Webhook を返さないための番兵値


def read_raw(path: Path | None = None) -> dict:
    """config.json をそのまま辞書で読む（既定値で補完）。"""
    path = path or CONFIG_FILE
    raw: dict = {}
    if path.exists():
        # 壊れたまま既定値で補完すると、UI から保存したときに手書きの設定が失われるためエラーにする。
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} の JSON が不正です: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} はオブジェクトである必要があります")
        raw = loaded
    raw.setdefault("watch", list(DEFAULT_WATCH))
    raw.setdefault("alert_threshold", DEFAULT_THRESHOLD)
    raw.setdefault("base_currency", "usd")
    raw.setdefault("retention_days", DEFAULT_RETENTION_DAYS)
    raw.setdefault("poll_interval", DEFAULT_POLL_INTERVAL)
    raw.setdefault("report_interval", DEFAULT_REPORT_INTERVAL)
    web = raw.get("web") if isinstance(raw.get("web"), dict) else {}
    raw["web"] = {
        **web,
        "host": web.get("host", DEFAULT_WEB_HOST),
        "port": web.get("port", DEFAULT_WEB_PORT),
        "auth_token": web.get("auth_token", ""),
        "protect_read": web.get("protect_read", True),
    }
    return raw


def masked_view(raw: dict) -> dict:
    """UI へ返す用に秘匿値を伏せた辞書を作る。"""
    view = json.loads(json.dumps(raw))  # deep copy
    view["webhook_configured"] = bool(raw.get("webhook_url"))
    view["webhook_url"] = ""  # 生の URL は返さない
    web = view.get("web") or {}
    web["auth_configured"] = bool(web.get("auth_token"))
    web["auth_token"] = ""
    view["web"] = web
    return view


# ユーザー情報・ポート・クエリ・フラグメント・バックスラッシュを含む URL は、
# 検証側と送信側（requests / urllib3）でホストの解釈が食い違い得るため、正規形だけを許可する。
WEBHOOK_RE = re.compile(
    r"https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
    r"/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
)
MIN_TOKEN_LENGTH = 12


def validate_webhook_url(url: str) -> None:
    """UI から設定できる Webhook を Discord に限る（任意の宛先へ POST させる踏み台を防ぐ）。"""
    if not WEBHOOK_RE.fullmatch(url):
        raise ConfigError("Webhook URL は https://discord.com/api/webhooks/<ID>/<トークン> の形式である必要があります")


def merge_incoming(existing: dict, incoming: dict) -> dict:
    """UI から来た設定を既存にマージ（空の秘匿値は現状維持）。

    待ち受けアドレス・ポートは UI からは変更できない（config.json か環境変数で設定する）。
    """
    merged = json.loads(json.dumps(existing))
    for key in (
        "alert_threshold", "base_currency", "retention_days", "alert_window", "alert_cooldown",
        "poll_interval", "report_interval", "watch",
    ):
        if key in incoming:
            merged[key] = incoming[key]

    # Webhook: 空文字なら現状維持、値があれば更新。
    new_hook = str(incoming.get("webhook_url", "")).strip()
    if new_hook and new_hook != WEBHOOK_MASK:
        validate_webhook_url(new_hook)
        merged["webhook_url"] = new_hook

    inc_web = incoming.get("web") if isinstance(incoming.get("web"), dict) else {}
    web = merged.get("web") if isinstance(merged.get("web"), dict) else {}
    if "protect_read" in inc_web:
        web["protect_read"] = _as_bool(inc_web["protect_read"])
    new_token = str(inc_web.get("auth_token", "")).strip()
    if new_token:
        if len(new_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"Web 認証トークンは {MIN_TOKEN_LENGTH} 文字以上にしてください")
        web["auth_token"] = new_token
    merged["web"] = web
    return merged


def write_raw(raw: dict, path: Path | None = None) -> None:
    """検証してから config.json を保存する。"""
    path = path or CONFIG_FILE
    parse_config(raw)  # 不正なら ConfigError を送出
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Webhook / トークンを含むため umask に依らず 600 で作る（残骸があれば作り直す）。
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(raw, indent=2, ensure_ascii=False))
    tmp.replace(path)
