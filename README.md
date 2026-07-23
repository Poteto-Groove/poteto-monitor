# poteto-monitor

設定ファイル駆動の **マルチ通貨モニター**。暗号資産（BTC / ETH / SOL …）と
為替レート（**ドル円 USD/JPY** など）を 1 時間ごとに取得し、Discord へ定期レポートを
送信します。急激な変動時にはアラートも飛ばします。

Proxmox CT (Debian 12) 上での常駐運用を想定した軽量ツールです。

> **v2 での変更点** — 監視対象をハードコードから **`config.json` の `watch` リスト駆動**に刷新。
> 暗号資産に加えて **法定通貨の為替レート**を扱えるようになり、アセットごとに閾値を設定可能。
> スクリプト単体から **Python パッケージ + console script** 構成へモダン化しました。
> （旧 `python monitor.py` も互換シムでそのまま動きます。）

## 特長

- 🪙 **暗号資産** — CoinGecko の任意の銘柄を USD / JPY など複数通貨で表示
- 💱 **為替レート** — ドル円・ユーロ円など任意の法定通貨ペア（API キー不要）
- ⚙️ **設定ファイル駆動** — 監視対象は `config.json` に足すだけ。コード変更不要
- 🚨 **アセット別アラート閾値** — 「暗号資産は 10%、為替は 2%」のような個別設定
- 🔔 **Discord Webhook** — 定期レポート + 急変アラートの embed 通知
- 🕒 **systemd タイマー** — 毎時実行。ハードニング済みユニット

## 動作環境

- **Proxmox VE** 上の LXC コンテナ（CT） … もちろん通常の Linux ホストでも可
- **OS**: Debian 12 (Bookworm) 以降
- **Python**: 3.11 以上（Debian 12 標準）
- **ネットワーク**: `api.coingecko.com` と `open.er-api.com` への HTTPS 疎通（いずれも API キー不要）

### 推奨 CT スペック

| 項目 | 最小 | 推奨 |
|---|---|---|
| CPU | 1 コア | 1 コア |
| RAM | 256 MB | 512 MB |
| ディスク | 2 GB | 4 GB |
| スワップ | 0 MB | 256 MB |
| 特権コンテナ | 不要（非特権 CT 推奨） | — |

## 構成

```
poteto-monitor/
├── poteto_monitor/           # 本体パッケージ
│   ├── config.py             #   設定の読み込み・検証
│   ├── providers.py          #   価格取得（crypto = CoinGecko / forex = open.er-api.com）
│   ├── notify.py             #   Discord embed 生成・送信
│   ├── format.py             #   通貨・変化率の整形
│   ├── storage.py            #   prices.json / history.json の入出力
│   └── monitor.py            #   オーケストレーション + CLI
├── config.example.json       # 設定サンプル
├── pyproject.toml            # パッケージ定義（console script: poteto-monitor）
├── monitor.py                # 後方互換シム（旧 `python monitor.py` 用）
├── poteto-monitor.service    # systemd サービス
├── poteto-monitor.timer      # systemd タイマー（毎時実行）
├── install.sh                # インストールスクリプト
└── tests/                    # 単体テスト（pytest）
```

実行時に生成されるデータ（`/var/lib/poteto-monitor/`）:

| ファイル | 内容 |
|---|---|
| `config.json` | Webhook URL・監視リスト・閾値設定 |
| `prices.json` | 最新の代表値（前回比較用） |
| `history.json` | 過去 `history_limit` 件の履歴（既定 168 = 7 日分） |

## セットアップ

### 1. リポジトリを取得

```bash
apt-get install -y git
git clone https://github.com/Poteto-Groove/poteto-monitor.git
cd poteto-monitor
```

### 2. インストール

```bash
apt-get install -y sudo
chmod +x install.sh
sudo ./install.sh
```

`install.sh` は専用ユーザー `poteto` を作成し、`/opt/poteto-monitor/venv` にパッケージを
インストール、systemd タイマーを有効化します。初回のみ `config.example.json` を
`/var/lib/poteto-monitor/config.json` にコピーします。

### 3. 設定を編集

```bash
sudo nano /var/lib/poteto-monitor/config.json
```

Discord の Webhook URL と、監視したい通貨を設定します（下記「設定」参照）。

### 4. 動作確認

```bash
# 送信せずに取得だけ試す（おすすめ）
sudo -u poteto /opt/poteto-monitor/venv/bin/poteto-monitor --dry-run

# 実際に 1 回実行して Discord に通知
sudo systemctl start poteto-monitor.service

# ログ / タイマー確認
sudo journalctl -u poteto-monitor.service -f
sudo systemctl list-timers poteto-monitor.timer
```

## 設定

`config.json` の例（`config.example.json` に同梱）:

```json
{
  "webhook_url": "https://discord.com/api/webhooks/xxxx/yyyy",
  "alert_threshold": 10,
  "base_currency": "usd",
  "history_limit": 168,
  "watch": [
    { "type": "crypto", "id": "bitcoin",  "label": "Bitcoin (BTC)",  "emoji": "🟡", "vs": ["usd", "jpy"] },
    { "type": "crypto", "id": "ethereum", "label": "Ethereum (ETH)", "emoji": "🔷", "vs": ["usd", "jpy"] },
    { "type": "crypto", "id": "solana",   "label": "Solana (SOL)",   "emoji": "🌞", "vs": ["usd", "jpy"], "threshold": 15 },
    { "type": "forex",  "base": "USD", "quote": "JPY", "label": "ドル円 (USD/JPY)",  "emoji": "💴", "threshold": 2 },
    { "type": "forex",  "base": "EUR", "quote": "JPY", "label": "ユーロ円 (EUR/JPY)", "emoji": "💶", "threshold": 2 }
  ]
}
```

### トップレベル項目

| キー | 既定 | 説明 |
|---|---|---|
| `webhook_url` | （必須） | Discord Webhook URL |
| `alert_threshold` | `10` | 既定のアラート閾値（%）。アセット側で上書き可 |
| `base_currency` | `"usd"` | 変化率の基準にする通貨 |
| `history_limit` | `168` | `history.json` に残す件数 |
| `watch` | BTC/ETH/ドル円 | 監視対象リスト（未指定なら既定 3 件） |

### `watch` エントリ

**暗号資産 (`type: "crypto"`)**

| キー | 必須 | 説明 |
|---|---|---|
| `id` | ✅ | CoinGecko の ID（例 `bitcoin`, `ethereum`, `solana`）|
| `label` / `emoji` | | 表示名・絵文字 |
| `vs` | | 表示通貨の配列（既定 `["usd","jpy"]`）|
| `threshold` | | このアセット固有のアラート閾値（%）|

**為替 (`type: "forex"`)**

| キー | 必須 | 説明 |
|---|---|---|
| `base` / `quote` | ✅ | 通貨ペア（例 `USD` / `JPY`）。`"pair": "USD/JPY"` 形式も可 |
| `label` / `emoji` | | 表示名・絵文字 |
| `threshold` | | このアセット固有のアラート閾値（%）|

> CoinGecko の銘柄 ID は [coins/list](https://api.coingecko.com/api/v3/coins/list) で確認できます。
> 為替レートは [open.er-api.com](https://www.exchangerate-api.com/docs/free) のオープンエンドポイントを使用（キー不要）。

### 環境変数での上書き

CI やコンテナ環境では、`config.json` の値を環境変数で上書きできます:

| 環境変数 | 対応する設定 |
|---|---|
| `DISCORD_WEBHOOK_URL` | `webhook_url` |
| `ALERT_THRESHOLD` | `alert_threshold` |
| `BASE_CURRENCY` | `base_currency` |
| `POTETO_DATA_DIR` | データ保存先（既定 `/var/lib/poteto-monitor`）|

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
poteto-monitor --dry-run
```

## 通知例

**定期レポート（毎時）**
> 📊 マーケット定期レポート
> 🟡 Bitcoin (BTC): **$103,240.00 (¥15,486,000)** 📈 前回比: +1.23%
> 🔷 Ethereum (ETH): **$3,842.00 (¥576,300)** ➡️ 前回比: -0.05%
> 💴 ドル円 (USD/JPY): **¥157.23 / 1 USD** 📈 前回比: +0.34%

**アラート（閾値超え）**
> 🚨 大きな変動を検知しました！
> 🚀 **Bitcoin (BTC)**: +12.34% → $118,900.00 (¥17,835,000)

## 開発

```bash
pip install -e ".[dev]"
pytest                 # 単体テスト（ネットワークはモック）
python -m poteto_monitor --dry-run
```

## あとがき

最近 ETH の動向を確認する機会が増えていて、友人も確認できたら良いということで作った小さなツールでした。

……が、気付けば銀行の COBOL のように、コミュニティで誰にもメンテされないまま静かに動き続けていました。
今回はそれを掘り起こして、通貨を自由に足せるように・ドル円などの為替も見られるように・
スタックもモダンに整え、README も刷新した「令和の改修」です。

- 皆様と自分の幸運を祈っております 🥔
