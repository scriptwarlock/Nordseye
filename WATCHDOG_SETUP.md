# Nordseye — Auto-restart / Watchdog Setup

---

## Server PC (Linux) — systemd

1. Copy the service file:
   ```bash
   sudo cp nordseye-server.service /etc/systemd/system/
   ```

2. Edit the paths inside it if yours differ from the defaults:
   ```bash
   sudo nano /etc/systemd/system/nordseye-server.service
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable nordseye-server   # auto-start on boot
   sudo systemctl start nordseye-server    # start now
   ```

4. Check status / logs:
   ```bash
   sudo systemctl status nordseye-server
   journalctl -u nordseye-server -f        # live log
   ```

---

## Client PCs (Linux) — systemd

1. Copy the service file to each client:
   ```bash
   sudo cp nordseye-agent.service /etc/systemd/system/
   ```

2. Edit the file — change `--server`, `--name`, `User`, and paths:
   ```bash
   sudo nano /etc/systemd/system/nordseye-agent.service
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable nordseye-agent
   sudo systemctl start nordseye-agent
   ```

4. Check logs:
   ```bash
   journalctl -u nordseye-agent -f
   ```

> **Note:** The service sets `DISPLAY=:0` so the tkinter overlay works.
> If your display is on a different seat, check with `echo $DISPLAY` while
> logged in and update the service file accordingly.

---

## Client PCs (Windows) — batch watchdog

1. Copy `nordseye-agent.bat` to each client PC (e.g. `C:\nordseye\`).

2. Edit the top three variables in the file:
   ```bat
   set AGENT_PATH=C:\nordseye\agent.py
   set SERVER=192.168.1.100
   set NAME=PC 01
   ```

3. **Auto-start on Windows login** — add to the Startup folder:
   - Press `Win + R`, type `shell:startup`, press Enter
   - Drop a shortcut to `nordseye-agent.bat` in that folder

4. To run it hidden (no console window), create a `.vbs` launcher in the
   same Startup folder:
   ```vbs
   Set WshShell = CreateObject("WScript.Shell")
   WshShell.Run "C:\nordseye\nordseye-agent.bat", 0, False
   ```

---

## Quick reference

| Command | What it does |
|---|---|
| `systemctl start nordseye-server` | Start server now |
| `systemctl stop nordseye-server` | Stop server |
| `systemctl restart nordseye-server` | Restart server |
| `journalctl -u nordseye-server -f` | Live server log |
| `journalctl -u nordseye-agent -f` | Live agent log |
