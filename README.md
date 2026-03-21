# poteto-monitor

Proxmox CT (Debian/Ubuntu) 上で動作する BTC/ETH 価格監視ツール。  
1時間ごとに価格を取得し Discord へ通知します。10% 以上の変動時はアラートも送信します。

## 構成

```
poteto-monitor/
├── monitor.py                # メインスクリプト
├── requirements.txt
├── poteto-monitor.service    # systemd サービス
├── poteto-monitor.timer      # systemd タイマー（毎時実行）
├── install.sh                # インストールスクリプト
└── README.md
```

実行時に生成されるデータ（`/var/lib/poteto-monitor/`）:

| ファイル | 内容 |
|---|---|
| `config.json` | Webhook URL・閾値設定 |
| `prices.json` | 最新価格（前回比較用） |
| `history.json` | 過去168時間（7日分）の価格履歴 |

## セットアップ

### 1. CT 上でリポジトリを取得

```bash
apt-get install -y git
git clone https://github.com/Poteto-Groove/poteto-monitor.git
cd poteto-monitor
```

### 2. インストール

```bash
chmod +x install.sh
sudo ./install.sh
```

### 3. Discord Webhook URL を設定

```bash
sudo nano /var/lib/poteto-monitor/config.json
```

```json
{
  "webhook_url": "https://discord.com/api/webhooks/xxxx/yyyy",
  "alert_threshold": 10
}
```

> **Discord Webhook の作成方法**: サーバー設定 → インテグレーション → ウェブフック → 新しいウェブフック

### 4. 動作確認

```bash
# 今すぐ実行
sudo systemctl start poteto-monitor.service

# ログ確認
sudo journalctl -u poteto-monitor.service -f

# タイマー状態確認
sudo systemctl list-timers poteto-monitor.timer
```

## 通知例

**定期レポート（毎時）**
> 📊 暗号資産 時間レポート  
> 🟡 Bitcoin (BTC): $103,240.00 (¥15,486,000) 📈 +1.23%  
> 🔷 Ethereum (ETH): $3,842.00 (¥576,300) ➡️ -0.05%

**アラート（10%以上の変動）**
> 🚨 大きな価格変動を検知しました！  
> 🚀 Bitcoin (BTC): +12.34%

## 環境変数で設定することも可能

`config.json` の代わりに環境変数でも設定できます（CI等での利用時）:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export ALERT_THRESHOLD=10
python monitor.py
```
