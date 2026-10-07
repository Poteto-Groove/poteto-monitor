"""共通データモデル。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Asset:
    """監視対象 1 件の定義（config.json の ``watch`` エントリ）。"""

    type: str  # "crypto" | "forex" | "hyperliquid" | "ratio"
    key: str  # 状態保存・履歴で使う一意キー
    label: str
    emoji: str
    threshold: float  # このアセット固有のアラート閾値 (%)

    # crypto 用 -----------------------------------------------------------
    coin_id: str = ""  # CoinGecko の ID (例: "bitcoin")
    vs: tuple[str, ...] = ()  # 表示する通貨 (例: ("usd", "jpy"))

    # forex 用 ------------------------------------------------------------
    base: str = ""  # 基準通貨 (例: "USD")
    quote: str = ""  # 相手通貨 (例: "JPY")

    # hyperliquid 用 ------------------------------------------------------
    coin: str = ""  # Hyperliquid の銘柄シンボル (例: "HYPE", "BTC")
    market: str = "perp"  # 現状は perp mid のみ対応

    # ratio 用（2 アセット間レート）---------------------------------------
    num: str = ""  # 分子の CoinGecko ID (例: "jpyc")
    den: str = ""  # 分母の CoinGecko ID (例: "usd-coin")


@dataclass
class Reading:
    """取得結果 1 件。表示と変化率計算の両方に使う。"""

    key: str
    label: str
    emoji: str
    value: float  # 変化率計算に使う代表値
    display: str  # Discord に出す整形済み文字列
    threshold: float
    type: str = ""  # "crypto" | "forex"
    fields: dict[str, float] = field(default_factory=dict)  # 履歴保存用の生値
    as_of: str | None = None  # 上流データの時刻（ISO 8601, 分かる場合のみ）
