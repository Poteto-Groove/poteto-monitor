#!/usr/bin/env bash
# poteto-monitor インストールスクリプト
# Debian/Ubuntu ベースの Proxmox CT で実行してください。
set -euo pipefail

INSTALL_DIR="/opt/poteto-monitor"
DATA_DIR="/var/lib/poteto-monitor"
SERVICE_USER="poteto"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== poteto-monitor インストール ==="

# 依存パッケージ
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# 専用ユーザー作成
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "ユーザー '$SERVICE_USER' を作成しました"
fi

# ファイル配置（パッケージ一式をコピー）
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp -r "$SRC_DIR/poteto_monitor" "$SRC_DIR/pyproject.toml" "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

# venv + パッケージインストール（Web ダッシュボード込み）
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q "$INSTALL_DIR[web]"

# config.json の作成（未存在時のみ）
CONFIG_FILE="$DATA_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$SRC_DIR/config.example.json" "$CONFIG_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    echo ""
    echo "⚠️  $CONFIG_FILE を編集して webhook_url と監視リストを設定してください！"
    echo ""
fi

# systemd ユニット配置
# - poteto-monitor-web.service : 常駐（Web ダッシュボード + ポーラー + Discord 通知）※既定で有効化
# - poteto-monitor.service/.timer : 毎時 1 回だけ通知する軽量モード（Web 不要な人向け・既定では無効）
cp "$SRC_DIR/poteto-monitor-web.service" /etc/systemd/system/
cp "$SRC_DIR/poteto-monitor.service" "$SRC_DIR/poteto-monitor.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now poteto-monitor-web.service

# 読み出したポート番号（表示用）
WEB_PORT="$(python3 -c "import json,sys; print(json.load(open('$CONFIG_FILE')).get('web',{}).get('port',8787))" 2>/dev/null || echo 8787)"

echo ""
echo "=== インストール完了 ==="
echo "設定ファイル      : $CONFIG_FILE"
echo "データディレクトリ: $DATA_DIR"
echo "Web ダッシュボード: http://127.0.0.1:${WEB_PORT}  （Cloudflare Tunnel で公開）"
echo ""
echo "状態確認:"
echo "  systemctl status poteto-monitor-web.service"
echo "  journalctl -u poteto-monitor-web.service -f"
echo ""
echo "取得だけ試す（送信・保存なし）:"
echo "  sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/poteto-monitor --dry-run"
