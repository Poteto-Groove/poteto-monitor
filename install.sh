#!/usr/bin/env bash
# poteto-monitor インストールスクリプト
# Debian/Ubuntu ベースの Proxmox CT で実行してください
set -euo pipefail

INSTALL_DIR="/opt/poteto-monitor"
DATA_DIR="/var/lib/poteto-monitor"
SERVICE_USER="poteto"

echo "=== poteto-monitor インストール ==="

# 依存パッケージ
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# 専用ユーザー作成
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "ユーザー '$SERVICE_USER' を作成しました"
fi

# ファイル配置
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp monitor.py requirements.txt "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

# venv + 依存インストール
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# config.json の作成（未存在時のみ）
CONFIG_FILE="$DATA_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'EOF'
{
  "webhook_url": "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL",
  "alert_threshold": 10
}
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    echo ""
    echo "⚠️  $CONFIG_FILE を編集して webhook_url を設定してください！"
    echo ""
fi

# systemd ユニット配置
cp poteto-monitor.service poteto-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now poteto-monitor.timer

echo ""
echo "=== インストール完了 ==="
echo "設定ファイル : $CONFIG_FILE"
echo "データディレクトリ: $DATA_DIR"
echo ""
echo "今すぐテスト実行:"
echo "  systemctl start poteto-monitor.service"
echo "  journalctl -u poteto-monitor.service -f"
