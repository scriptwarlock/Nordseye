#!/usr/bin/env bash
# ─── Nordseye Client Agent Install Script ────────────────────────────────────
# Run this ONCE on each client PC to register and enable the agent as a service.
# Usage: chmod +x install-client.sh && sudo ./install-client.sh
#
# Optional env vars before running:
#   SERVER_IP=192.168.122.1  PC_NAME="PC 02"  PC_USER=pc1

set -e

SERVICE_NAME="nordseye-agent"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
SERVICE_SRC="${SCRIPT_DIR}/nordseye-agent.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

# Config overrides
SERVER_IP="${SERVER_IP:-192.168.122.1}"
PC_NAME="${PC_NAME:-$(hostname)}"
PC_USER="${PC_USER:-$(logname 2>/dev/null || echo $SUDO_USER)}"
AGENT_PATH="${SCRIPT_DIR}/agent.py"

echo "=== Nordseye Client Agent Installer ==="
echo "  Server IP : $SERVER_IP"
echo "  PC Name   : $PC_NAME"
echo "  Run as    : $PC_USER"
echo "  Agent     : $AGENT_PATH"
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] Please run as root: sudo ./install-client.sh"
    exit 1
fi

# Install websockets — prefer apt on Ubuntu/Debian
echo "[1/5] Installing Python dependencies..."
if apt-get install -y python3-websockets 2>/dev/null; then
    echo "   Installed via apt."
elif pip3 install --break-system-packages --quiet websockets 2>/dev/null; then
    echo "   Installed via pip (--break-system-packages)."
else
    pip3 install --quiet websockets || true
fi

# Generate service file from template
echo "[2/5] Writing service file..."
cat > "$SERVICE_DST" <<EOF
[Unit]
Description=Nordseye Client Agent
After=network.target graphical-session.target
Wants=network.target

[Service]
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/usr/bin/python3 ${AGENT_PATH} --server ${SERVER_IP} --name "${PC_NAME}"
Restart=always
RestartSec=5
StartLimitIntervalSec=0
StandardOutput=journal
StandardError=journal
User=${PC_USER}
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/${PC_USER}/.Xauthority

[Install]
WantedBy=graphical-session.target
EOF

chmod 644 "$SERVICE_DST"

# Reload & enable
echo "[3/5] Enabling service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "[4/5] Starting agent..."
systemctl restart "$SERVICE_NAME"

# Status
echo "[5/5] Done!"
systemctl --no-pager status "$SERVICE_NAME"
echo ""
echo "View logs : journalctl -u $SERVICE_NAME -f"
echo "Stop      : systemctl stop $SERVICE_NAME"
echo "Start     : systemctl start $SERVICE_NAME"
