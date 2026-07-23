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

    return Config(
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or raw.get("webhook_url", ""),
        alert_threshold=default_threshold,
        base_currency=str(os.environ.get("BASE_CURRENCY") or raw.get("base_currency", "usd")).lower(),
        history_limit=int(raw.get("history_limit", DEFAULT_HISTORY_LIMIT)),
        assets=assets,
    )


def load_config(path: Path = CONFIG_FILE) -> Config:
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} の JSON が不正です: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} はオブジェクトである必要があります")
    return parse_config(raw)
