"""設定の読み込みと検証。

優先順位: 環境変数 > config.json > 既定値。
config.json が無くても、既定の監視リスト（BTC / ETH / ドル円）で動作します。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import Asset

DATA_DIR = Path(os.environ.get("POTETO_DATA_DIR", "/var/lib/poteto-monitor"))
CONFIG_FILE = DATA_DIR / "config.json"
PRICES_FILE = DATA_DIR / "prices.json"
HISTORY_FILE = DATA_DIR / "history.json"

DEFAULT_THRESHOLD = 10.0
DEFAULT_HISTORY_LIMIT = 168  # 7 日分（毎時実行時）
DEFAULT_POLL_INTERVAL = 60  # 秒。Web ダッシュボードの更新間隔
MIN_POLL_INTERVAL = 5  # API のレート制限を守るための下限
DEFAULT_REPORT_INTERVAL = 3600  # 秒。Discord 定期レポートの間隔
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8787

# config.json が無い場合の既定監視リスト。
DEFAULT_WATCH: list[dict] = [
    {"type": "crypto", "id": "bitcoin", "label": "Bitcoin (BTC)", "emoji": "🟡", "vs": ["usd", "jpy"]},
    {"type": "crypto", "id": "ethereum", "label": "Ethereum (ETH)", "emoji": "🔷", "vs": ["usd", "jpy"]},
    {"type": "forex", "base": "USD", "quote": "JPY", "label": "ドル円 (USD/JPY)", "emoji": "💴", "threshold": 2},
]


class ConfigError(ValueError):
    """設定が不正なときに送出。"""


@dataclass
class Config:
    webhook_url: str
    alert_threshold: float
    base_currency: str
    history_limit: int
    assets: list[Asset]
    poll_interval: int = DEFAULT_POLL_INTERVAL
    report_interval: int = DEFAULT_REPORT_INTERVAL
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    web_auth_token: str = ""


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_asset(raw: dict, index: int, default_threshold: float) -> Asset:
    if not isinstance(raw, dict):
        raise ConfigError(f"watch[{index}] はオブジェクトである必要があります")

    atype = str(raw.get("type", "")).strip().lower()
    threshold = float(raw.get("threshold", default_threshold))

    if atype == "crypto":
        coin_id = str(raw.get("id", "")).strip().lower()
        if not coin_id:
            raise ConfigError(f"watch[{index}] (crypto) には 'id' が必要です")
        vs = tuple(str(c).strip().lower() for c in raw.get("vs", ["usd", "jpy"]) if str(c).strip())
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

    raise ConfigError(f"watch[{index}] の type '{atype}' は未対応です (crypto / forex)")


def parse_config(raw: dict) -> Config:
    """辞書から Config を組み立てる（環境変数の上書きも適用）。"""
    default_threshold = float(
        os.environ.get("ALERT_THRESHOLD") or raw.get("alert_threshold", DEFAULT_THRESHOLD)
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
    poll_interval = int(os.environ.get("POLL_INTERVAL") or raw.get("poll_interval", DEFAULT_POLL_INTERVAL))
    report_interval = int(raw.get("report_interval", DEFAULT_REPORT_INTERVAL))

    return Config(
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or raw.get("webhook_url", ""),
        alert_threshold=default_threshold,
        base_currency=str(os.environ.get("BASE_CURRENCY") or raw.get("base_currency", "usd")).lower(),
        history_limit=int(raw.get("history_limit", DEFAULT_HISTORY_LIMIT)),
        assets=assets,
        poll_interval=max(MIN_POLL_INTERVAL, poll_interval),
        report_interval=max(0, report_interval),
        web_host=str(os.environ.get("WEB_HOST") or web.get("host", DEFAULT_WEB_HOST)),
        web_port=int(os.environ.get("WEB_PORT") or web.get("port", DEFAULT_WEB_PORT)),
        web_auth_token=str(os.environ.get("WEB_AUTH_TOKEN") or web.get("auth_token", "")),
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
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except json.JSONDecodeError:
            raw = {}
    raw.setdefault("watch", list(DEFAULT_WATCH))
    raw.setdefault("alert_threshold", DEFAULT_THRESHOLD)
    raw.setdefault("base_currency", "usd")
    raw.setdefault("history_limit", DEFAULT_HISTORY_LIMIT)
    raw.setdefault("poll_interval", DEFAULT_POLL_INTERVAL)
    raw.setdefault("report_interval", DEFAULT_REPORT_INTERVAL)
    web = raw.get("web") if isinstance(raw.get("web"), dict) else {}
    raw["web"] = {
        "host": web.get("host", DEFAULT_WEB_HOST),
        "port": web.get("port", DEFAULT_WEB_PORT),
        "auth_token": web.get("auth_token", ""),
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


def merge_incoming(existing: dict, incoming: dict) -> dict:
    """UI から来た設定を既存にマージ（空の秘匿値は現状維持）。"""
    merged = json.loads(json.dumps(existing))
    for key in ("alert_threshold", "base_currency", "history_limit", "poll_interval", "report_interval", "watch"):
        if key in incoming:
            merged[key] = incoming[key]

    # Webhook: 空文字なら現状維持、値があれば更新。
    new_hook = str(incoming.get("webhook_url", "")).strip()
    if new_hook and new_hook != WEBHOOK_MASK:
        merged["webhook_url"] = new_hook

    inc_web = incoming.get("web") if isinstance(incoming.get("web"), dict) else {}
    web = merged.get("web") if isinstance(merged.get("web"), dict) else {}
    for key in ("host", "port"):
        if key in inc_web:
            web[key] = inc_web[key]
    new_token = str(inc_web.get("auth_token", "")).strip()
    if new_token:
        web["auth_token"] = new_token
    merged["web"] = web
    return merged


def write_raw(raw: dict, path: Path | None = None) -> None:
    """検証してから config.json を保存する。"""
    path = path or CONFIG_FILE
    parse_config(raw)  # 不正なら ConfigError を送出
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
