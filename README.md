# Nordseye Cyber Management
## Setup Guide

### Requirements
- Python 3.8+ on the **server PC** (the one running the dashboard)
- Python 3.8+ on each **client/customer PC**
- All PCs on the **same local network** (LAN / Wi-Fi)

---

## STEP 1 — Install Python dependencies

**On the server PC and all client PCs:**
```bash
pip install websockets
```

---

## STEP 2 — Start the server

Run this on your **server/cashier PC**:
```bash
python server.py
```

You'll see:
```
  Agent port : 8765  (client PCs connect here)
  UI port    : 8766  (browser dashboard)
```

Find your server's local IP:
- Windows: `ipconfig` → look for IPv4 Address (e.g. 192.168.1.100)
- Linux:   `ip addr` or `hostname -I`

---

## STEP 3 — Open the dashboard

Open `index.html` in a browser on the server PC, **or** open:
```
http://192.168.1.100:8766/
```
from any device on the network (replace with your server IP).

Default login: **admin / 1234**

---

## STEP 4 — Run agent on each customer PC

Copy `agent.py` to each customer PC, then run:

**Windows:**
```cmd
python agent.py --server 192.168.1.100 --name "PC 01"
```

**Linux:**
```bash
python3 agent.py --server 192.168.1.100 --name "PC 01"
```

Change `192.168.1.100` to your actual server IP.
Change `"PC 01"` to a descriptive name for that PC.

The agent will:
- Automatically appear in the Terminals grid on the dashboard
- Show a small overlay window in the corner with session status and owed amount
- Lock/unlock the screen when commanded from the dashboard
- Reconnect automatically if the server restarts

---

## Auto-start agent on boot (optional)

**Windows** — create a `.bat` file and add to Startup folder:
```bat
@echo off
python C:\nordseye\agent.py --server 192.168.1.100 --name "PC 01"
```

**Linux** — add to `/etc/rc.local` or create a systemd service:
```bash
[Unit]
Description=Nordseye Agent

[Service]
ExecStart=/usr/bin/python3 /opt/nordseye/agent.py --server 192.168.1.100 --name "PC 01"
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## File summary

| File         | Purpose                                      |
|--------------|----------------------------------------------|
| `server.py`  | Run on server PC — manages all sessions      |
| `agent.py`   | Run on each customer PC — reports + locks    |
| `index.html` | Dashboard — open in any browser              |

---

## Network diagram

```
  [Browser / Dashboard]
         |
    port 8766 (WebSocket)
         |
   [server.py]  ← runs on your cashier/server PC
         |
    port 8765 (WebSocket)
    /    |    \
[PC 01] [PC 02] [PC 03] ...  ← agent.py on each
```

All communication is on your local network only.
