# poteto-monitor

Proxmox CT (Debian 12) 上で動作する BTC/ETH 価格監視ツール。  
1時間ごとに価格を取得し Discord へ通知します。10% 以上の変動時はアラートも送信します。

## 動作環境

- **Proxmox VE** 上の LXC コンテナ（CT）
- **OS**: Debian 12 (Bookworm)
- **Python**: 3.11 以上（Debian 12 標準）

## 推奨 CT スペック

| 項目 | 最小 | 推奨 |
|---|---|---|
| CPU | 1 コア | 1 コア |
| RAM | 256 MB | 512 MB |
| ディスク | 2 GB | 4 GB |
| スワップ | 0 MB | 256 MB |
| ネットワーク | NIC × 1（インターネット疎通必須） | — |
| 特権コンテナ | 不要（非特権 CT 推奨） | — |

> **ディスク内訳の目安**  
> Debian 12 最小構成 約 600 MB + Python venv 約 100 MB + データファイル（history.json 等）数 MB  
> 余裕を持って 2 GB 以上を推奨。ログを長期保持する場合は 4 GB 以上。

### Proxmox CT 作成手順（概要）

1. Proxmox の「ローカルストレージ」→「CT テンプレート」から `debian-12-standard` をダウンロード
2. 「CT 作成」で以下を設定してコンテナを作成

   ```
   テンプレート : debian-12-standard_*.tar.zst
   ディスク     : 4 GB（rootfs）
   CPU          : 1 コア
   メモリ       : 512 MB
   スワップ     : 256 MB
   ネットワーク : DHCP または固定 IP（インターネット疎通必須）
   ```

3. CT を起動し、コンソールにログイン

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
apt-get install -y sudo
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
###
最近ETHの動向を確認する機会が増えていて、友人も確認できたら良いということで作成しました。
