#!/usr/bin/env bash
# ─── Nordseye Server Install Script ──────────────────────────────────────────
# Run this ONCE on the server machine to register and enable the systemd service.
# Usage: chmod +x install-server.sh && ./install-server.sh

set -e

SERVICE_NAME="nordseye-server"
SERVICE_SRC="$(dirname "$(realpath "$0")")/nordseye-server.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== Nordseye Server Installer ==="

if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] Please run as root: sudo ./install-server.sh"
    exit 1
fi

# Install websockets — prefer apt on Ubuntu/Debian
echo "[1/4] Installing Python dependencies..."
if apt-get install -y python3-websockets 2>/dev/null; then
    echo "   Installed via apt."
elif pip3 install --break-system-packages --quiet websockets 2>/dev/null; then
    echo "   Installed via pip (--break-system-packages)."
else
    pip3 install --quiet websockets || true
fi

# Copy service file
echo "[2/4] Installing systemd service..."
cp "$SERVICE_SRC" "$SERVICE_DST"
chmod 644 "$SERVICE_DST"

# Reload & enable
echo "[3/4] Enabling and starting service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# Status
echo "[4/4] Done!"
systemctl --no-pager status "$SERVICE_NAME"
echo ""
echo "View logs : journalctl -u $SERVICE_NAME -f"
echo "Stop      : systemctl stop $SERVICE_NAME"
echo "Start     : systemctl start $SERVICE_NAME"
