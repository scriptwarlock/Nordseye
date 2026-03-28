#!/usr/bin/env python3
"""
Sync-Space Cyber Cafe Server
--------------------------
Manages client PCs over WebSocket.
Run: python server.py
Default ports: 8765 (client agents), 8766 (web UI browser)
"""

import asyncio
import json
import time
import uuid
import hashlib
import logging
import os
import sys
import sqlite3
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from websockets.http11 import Response
from websockets.datastructures import Headers
from typing import Dict, Optional

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:
    print("[ERROR] Missing dependency. Run: pip install websockets")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("nordseye")

# ─── Config ──────────────────────────────────────────────────────────────────
AGENT_PORT  = 8765   # client PCs connect here (pure WebSocket)
UI_PORT     = 8766   # browser dashboard: serves index.html via HTTP + WebSocket
TICK_SECS   = 5      # how often to push updates to UI

# Resolve index.html relative to this script
_SCRIPT_DIR = Path(__file__).resolve().parent
_INDEX_HTML  = _SCRIPT_DIR / "index.html"

# ─── In-memory state ─────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.clients: Dict[str, dict] = {}          # pc_id -> PC state
        self.agent_sockets: Dict[str, object] = {}  # pc_id -> websocket
        self.ui_sockets: set = set()                # browser dashboard sockets
        self.sessions: list = []                    # completed sessions log
        self.product_log: list = []
        self.expenses: list = []
        self.tariffs = [
            {"id":"t1","name":"Regular",    "hourPrice":20,"freeAfter":10,"days":127},
            {"id":"t2","name":"Student",    "hourPrice":15,"freeAfter":10,"days":62},
            {"id":"t3","name":"Night",       "hourPrice":12,"freeAfter":10,"days":127},
            {"id":"t4","name":"Gaming",      "hourPrice":25,"freeAfter":15,"days":127},
        ]
        self.products = [
            {"id":"p1","name":"Mineral Water","category":"Drinks", "price":15,"stock":50},
            {"id":"p2","name":"Soft Drink",   "category":"Drinks", "price":20,"stock":30},
            {"id":"p3","name":"Chips",         "category":"Snacks", "price":12,"stock":40},
            {"id":"p4","name":"Print/page",    "category":"Services","price":5, "stock":9999},
        ]
        self.members = []
        self.tickets = []
        self.employees = [
            {"id":"emp1","name":"Admin","username":"admin",
             "password": self._hash("1234"),"role":"admin","active":True},
        ]
        self.settings = {
            "cafeName": "Nordseye CyberCafe",
            "address":  "Cebu City",
            "currency": "₱",
            "tax": 0,
        }

    def _hash(self, pwd: str) -> str:
        return hashlib.sha256(pwd.encode()).hexdigest()

    def validate_employee(self, username: str, password: str) -> Optional[dict]:
        h = self._hash(password)
        for e in self.employees:
            if e["username"] == username and e["password"] == h and e["active"]:
                return e
        return None

    def get_tariff(self, tid: str) -> dict:
        return next((t for t in self.tariffs if t["id"] == tid), self.tariffs[0])

    def calc_owed(self, pc: dict) -> float:
        if pc["status"] not in ("active", "paused") or not pc.get("startTime"):
            return 0.0
        paused_total = pc.get("pausedTotal", 0.0)
        # If currently paused, add additional pause time not yet committed
        if pc["status"] == "paused" and pc.get("pausedAt"):
            paused_total += time.time() - pc["pausedAt"]
        elapsed_min = (time.time() - pc["startTime"] - paused_total) / 60.0
        elapsed_min = max(0.0, elapsed_min)
        tariff = self.get_tariff(pc.get("tariff","t1"))
        billable = max(0, elapsed_min - tariff.get("freeAfter", 0))
        cost = (billable / 60.0) * tariff["hourPrice"]
        prod_cost = sum(p["price"]*p["amount"] for p in pc.get("products",[]))
        return round(cost + prod_cost, 2)

    def register_pc(self, pc_id: str, info: dict):
        if pc_id not in self.clients:
            self.clients[pc_id] = {
                "id":       pc_id,
                "name":     info.get("name", pc_id),
                "platform": info.get("platform", "unknown"),
                "ip":       info.get("ip", ""),
                "status":   "inactive",
                "startTime": None,
                "tariff":   "t1",
                "member":   None,
                "products": [],
                "connected": True,
                "lastSeen": time.time(),
            }
            log.info(f"New PC registered: {pc_id} ({info.get('name','?')})")
        else:
            self.clients[pc_id]["connected"] = True
            self.clients[pc_id]["lastSeen"] = time.time()
            self.clients[pc_id]["ip"] = info.get("ip", self.clients[pc_id]["ip"])

    def start_session(self, pc_id: str, tariff_id: str, member_id: Optional[str]) -> bool:
        if pc_id not in self.clients:
            return False
        pc = self.clients[pc_id]
        if pc["status"] != "inactive":
            return False
        pc.update({
            "status":     "active",
            "startTime":  time.time(),
            "tariff":     tariff_id,
            "member":     member_id,
            "products":   [],
            "session_id": str(uuid.uuid4()),
            "pausedTotal": 0.0,
            "pausedAt":   None,
        })
        log.info(f"Session started: {pc_id}, tariff={tariff_id}")
        return True

    def stop_session(self, pc_id: str) -> Optional[dict]:
        if pc_id not in self.clients:
            return None
        pc = self.clients[pc_id]
        if pc["status"] == "inactive":
            return None
        owed = self.calc_owed(pc)
        duration = int((time.time() - (pc["startTime"] or time.time())) / 60)
        session = {
            "id":       str(uuid.uuid4()),
            "terminal": pc_id,
            "member":   pc.get("member"),
            "tariff":   pc.get("tariff","t1"),
            "stime":    pc.get("startTime", time.time()) * 1000,
            "etime":    time.time() * 1000,
            "duration": duration,
            "price":    owed,
            "products": list(pc.get("products", [])),
        }
        self.sessions.insert(0, session)
        pc.update({
            "status":     "inactive",
            "startTime":  None,
            "member":     None,
            "products":   [],
            "session_id": None,
            "pausedTotal": 0.0,
            "pausedAt":   None,
        })
        pc.pop("timeout", None)
        log.info(f"Session stopped: {pc_id}, owed={owed}")
        return session

    def pause_session(self, pc_id: str):
        if pc_id in self.clients:
            pc = self.clients[pc_id]
            if pc["status"] == "active":
                pc["status"]   = "paused"
                pc["pausedAt"] = time.time()
            elif pc["status"] == "paused":
                # commit paused duration
                if pc.get("pausedAt"):
                    pc["pausedTotal"] = pc.get("pausedTotal", 0.0) + (time.time() - pc["pausedAt"])
                    pc["pausedAt"] = None
                pc["status"] = "active"

    def validate_member(self, username: str, password: str) -> Optional[dict]:
        h = self._hash(password)
        for m in self.members:
            if m.get("username") == username and m.get("password") == h and m.get("active", True):
                return m
        return None

    def snapshot(self) -> dict:
        """Full state snapshot for browser UI."""
        pcs = []
        for pc in self.clients.values():
            pcs.append({**pc, "owed": self.calc_owed(pc)})
        return {
            "type":       "state",
            "clients":    pcs,
            "tariffs":    self.tariffs,
            "products":   self.products,
            "members":    self.members,
            "tickets":    self.tickets,
            "employees":  [
                {k:v for k,v in e.items() if k != "password"}
                for e in self.employees
            ],
            "sessions":   self.sessions[:200],
            "productLog": self.product_log[:200],
            "expenses":   self.expenses[:200],
            "settings":   self.settings,
            "serverTime": int(time.time() * 1000),
        }

store = Store()
DB_FILE = _SCRIPT_DIR / "system.db"
_last_saved_json = ""

def load_store(store: Store):
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("CREATE TABLE IF NOT EXISTS server_state (id INTEGER PRIMARY KEY, data TEXT)")
        row = con.execute("SELECT data FROM server_state WHERE id=1").fetchone()
        con.close()
        if row:
            data = json.loads(row[0])
            store.members = data.get("members", [])
            store.tickets = data.get("tickets", [])
            store.products = data.get("products", [])
            store.tariffs = data.get("tariffs", store.tariffs)
            store.sessions = data.get("sessions", [])
            store.employees = data.get("employees", store.employees)
            store.product_log = data.get("product_log", [])
            store.expenses = data.get("expenses", [])
            store.settings = data.get("settings", store.settings)
            global _last_saved_json
            _last_saved_json = json.dumps(data)
            log.info("Loaded server state from system.db")
    except Exception as e:
        log.error(f"Failed to load DB: {e}")

def save_store(store: Store):
    global _last_saved_json
    try:
        data = {
            "members": store.members,
            "tickets": store.tickets,
            "products": store.products,
            "tariffs": store.tariffs,
            "sessions": store.sessions,
            "employees": store.employees,
            "product_log": store.product_log,
            "expenses": store.expenses,
            "settings": store.settings,
        }
        j = json.dumps(data)
        if j == _last_saved_json: return
        _last_saved_json = j
        con = sqlite3.connect(DB_FILE)
        con.execute("CREATE TABLE IF NOT EXISTS server_state (id INTEGER PRIMARY KEY, data TEXT)")
        con.execute("INSERT OR REPLACE INTO server_state (id, data) VALUES (1, ?)", (j,))
        con.commit()
        con.close()
    except Exception as e:
        log.error(f"Failed to save DB: {e}")

load_store(store)

# ─── HTTP handler for the UI port ────────────────────────────────────────────
async def process_ui_request(connection, request):
    """
    Serve index.html for plain HTTP GET requests on the UI port.
    WebSocket upgrade requests are passed through unchanged.

    Compatible with websockets 14+ (new asyncio API):
      request.path    -> the request path
      request.headers -> Headers mapping (there is no .method attribute)
    WebSocket upgrades carry "Upgrade: websocket"; plain browser GETs do not.
    """
    upgrade = request.headers.get("Upgrade", "").lower()
    if upgrade == "websocket":
        return None  # let the normal WebSocket handshake proceed

    # Plain HTTP request — build a proper Response with text/html content-type
    try:
        body = _INDEX_HTML.read_bytes()
        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
        ])
        return Response(HTTPStatus.OK, "OK", headers, body)
    except FileNotFoundError:
        body = b"<h1>index.html not found - place it next to server.py</h1>"
        headers = Headers([
            ("Content-Type", "text/html"),
            ("Content-Length", str(len(body))),
        ])
        return Response(HTTPStatus.NOT_FOUND, "Not Found", headers, body)


async def send_to_agent(pc_id: str, msg: dict):
    ws = store.agent_sockets.get(pc_id)
    if ws:
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass

# ─── Broadcast state to all UI browsers ──────────────────────────────────────
async def broadcast_ui(msg: dict = None):
    if not store.ui_sockets:
        return
    data = json.dumps(msg or store.snapshot())
    dead = set()
    for ws in store.ui_sockets:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    store.ui_sockets -= dead

# ─── Handle agent (client PC) connections ────────────────────────────────────
async def handle_agent(websocket: WebSocketServerProtocol):
    pc_id = None
    ip = websocket.remote_address[0] if websocket.remote_address else "?"
    log.info(f"Agent connected from {ip}")
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            if mtype == "register":
                pc_id = msg.get("id") or f"pc_{ip.replace('.','_')}"
                msg["ip"] = ip
                store.register_pc(pc_id, msg)
                store.agent_sockets[pc_id] = websocket
                # Tell agent its current state
                pc = store.clients[pc_id]
                await websocket.send(json.dumps({
                    "type":   "init",
                    "status": pc["status"],
                    "owed":   store.calc_owed(pc),
                    "startTime": pc.get("startTime"),
                    "settings": store.settings,
                }))
                await broadcast_ui()

            elif mtype == "heartbeat" and pc_id:
                store.clients[pc_id]["lastSeen"] = time.time()
                pc = store.clients[pc_id]
                # Send back current owed amount every heartbeat
                await websocket.send(json.dumps({
                    "type":   "tick",
                    "status": pc["status"],
                    "owed":   store.calc_owed(pc),
                    "timeLeft": _time_left(pc),
                    "startTime": pc.get("startTime"),
                }))

            elif mtype == "ticket_login":
                code = msg.get("code", "").upper().strip()
                ticket = next((t for t in store.tickets if t["code"] == code and t["status"] == "active"), None)
                if ticket:
                    if pc_id and pc_id in store.clients:
                        if store.clients[pc_id]["status"] == "inactive":
                            ticket["status"] = "used"
                            ticket["usedAt"] = time.time() * 1000
                            store.start_session(pc_id, ticket["tariff"], None)
                            store.clients[pc_id]["timeout"] = ticket["minutes"]
                            pc = store.clients[pc_id]
                            tariff_obj = store.get_tariff(pc["tariff"])
                            await send_to_agent(pc_id, {
                                "type":      "start",
                                "tariff":    pc["tariff"],
                                "hourPrice": tariff_obj.get("hourPrice", 0),
                                "status":    "active",
                                "timeout":   pc["timeout"],
                            })
                            await websocket.send(json.dumps({"type": "ticket_result", "ok": True}))
                            await broadcast_ui()
                        else:
                            await websocket.send(json.dumps({"type": "ticket_result", "ok": False, "err": "PC already active"}))
                else:
                    await websocket.send(json.dumps({"type": "ticket_result", "ok": False, "err": "Invalid or used ticket"}))

            elif mtype == "member_login":
                username = msg.get("username", "").strip()
                password = msg.get("password", "")
                member = store.validate_member(username, password)
                if member and pc_id and store.clients.get(pc_id, {}).get("status") == "inactive":
                    mid = member["id"]
                    tariff = member.get("tariff", "t1")
                    store.start_session(pc_id, tariff, mid)
                    pc = store.clients[pc_id]
                    tariff_obj = store.get_tariff(tariff)
                    await send_to_agent(pc_id, {
                        "type":      "start",
                        "tariff":    tariff,
                        "hourPrice": tariff_obj.get("hourPrice", 0),
                        "status":    "active",
                        "timeout":   None,
                    })
                    await websocket.send(json.dumps({"type": "member_login_result", "ok": True}))
                    await broadcast_ui()
                else:
                    await websocket.send(json.dumps({"type": "member_login_result", "ok": False, "err": "Invalid credentials or session already active"}))

            elif mtype == "client_action":
                action = msg.get("action")
                if action == "pause":
                    store.pause_session(pc_id)
                    pc = store.clients.get(pc_id, {})
                    await send_to_agent(pc_id, {"type": "pause", "status": pc.get("status","paused")})
                    await broadcast_ui()
                elif action == "stop":
                    session = store.stop_session(pc_id)
                    if session:
                        await send_to_agent(pc_id, {"type": "stop", "session": session})
                    await broadcast_ui()

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if pc_id and pc_id in store.clients:
            store.clients[pc_id]["connected"] = False
            store.agent_sockets.pop(pc_id, None)
            log.info(f"Agent disconnected: {pc_id}")
            await broadcast_ui()

def _time_left(pc: dict) -> Optional[float]:
    """Minutes remaining if a timeout was set."""
    if pc.get("timeout") and pc.get("startTime"):
        used = (time.time() - pc["startTime"]) / 60.0
        return max(0.0, float(pc["timeout"] - used))
    return None

# ─── Handle browser UI connections ───────────────────────────────────────────
async def handle_ui(websocket: WebSocketServerProtocol):
    ip = websocket.remote_address[0] if websocket.remote_address else "?"
    log.info(f"Browser UI connected from {ip}")
    store.ui_sockets.add(websocket)
    # Send full state immediately
    await websocket.send(json.dumps(store.snapshot()))
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_ui_command(msg, websocket)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        store.ui_sockets.discard(websocket)
        log.info(f"Browser UI disconnected: {ip}")

async def handle_ui_command(msg: dict, ws):
    cmd = msg.get("cmd")
    pc_id = msg.get("pcId")
    reply = {"type": "ack", "cmd": cmd, "ok": True}

    # ── Session control ──
    if cmd == "start":
        ok = store.start_session(pc_id, msg.get("tariff","t1"), msg.get("member"))
        if ok:
            pc = store.clients[pc_id]
            if msg.get("timeout"):
                pc["timeout"] = msg.get("timeout")
            else:
                pc.pop("timeout", None)
            tariff_obj = store.get_tariff(pc["tariff"])
            await send_to_agent(pc_id, {
                "type":      "start",
                "tariff":    pc["tariff"],
                "hourPrice": tariff_obj.get("hourPrice", 0),
                "status":    "active",
                "timeout":   pc.get("timeout"),
            })
        reply["ok"] = ok

    elif cmd == "stop":
        session = store.stop_session(pc_id)
        if session:
            await send_to_agent(pc_id, {"type": "stop", "session": session})
        reply["session"] = session

    elif cmd == "pause":
        store.pause_session(pc_id)
        pc = store.clients.get(pc_id, {})
        await send_to_agent(pc_id, {"type": "pause", "status": pc.get("status","paused")})

    elif cmd == "lock":
        await send_to_agent(pc_id, {"type": "lock"})

    elif cmd == "unlock":
        await send_to_agent(pc_id, {"type": "unlock"})

    elif cmd == "message":
        await send_to_agent(pc_id, {"type": "message", "text": msg.get("text","")})

    elif cmd == "message_all":
        for pid in store.clients:
            await send_to_agent(pid, {"type": "message", "text": msg.get("text","")})

    # ── Tariff CRUD ──
    elif cmd == "tariff_add":
        t = {**msg["tariff"], "id": str(uuid.uuid4())}
        store.tariffs.append(t)
    elif cmd == "tariff_edit":
        store.tariffs = [t if t["id"] != msg["tariff"]["id"] else msg["tariff"] for t in store.tariffs]
    elif cmd == "tariff_del":
        store.tariffs = [t for t in store.tariffs if t["id"] != msg["id"]]

    # ── Ticket CRUD ──
    elif cmd == "ticket_add":
        qty = msg.get("qty", 1)
        tariff = msg.get("tariff", "t1")
        minutes = msg.get("minutes", 60)
        price = msg.get("price", 0)
        import random, string
        for _ in range(qty):
            code = ''.join(random.choices(string.ascii_uppercase, k=6))
            store.tickets.append({
                "id": str(uuid.uuid4()),
                "code": code,
                "tariff": tariff,
                "minutes": minutes,
                "price": price,
                "status": "active",
                "createdAt": time.time() * 1000,
            })
    elif cmd == "ticket_del":
        store.tickets = [t for t in store.tickets if t["id"] != msg["id"]]

    # ── Product CRUD ──
    elif cmd == "product_add":
        p = {**msg["product"], "id": str(uuid.uuid4())}
        store.products.append(p)
    elif cmd == "product_edit":
        store.products = [p if p["id"] != msg["product"]["id"] else msg["product"] for p in store.products]
    elif cmd == "product_del":
        store.products = [p for p in store.products if p["id"] != msg["id"]]
    elif cmd == "product_sell":
        pc = store.clients.get(pc_id)
        prod = next((p for p in store.products if p["id"] == msg["productId"]), None)
        if pc and prod:
            amt = msg.get("amount", 1)
            existing = next((x for x in pc["products"] if x["id"] == prod["id"]), None)
            if existing:
                existing["amount"] += amt
            else:
                pc["products"].append({"id": prod["id"], "name": prod["name"], "price": prod["price"], "amount": amt})
            prod["stock"] = max(0, prod["stock"] - amt)
            store.product_log.insert(0, {
                "id": str(uuid.uuid4()), "terminal": pc_id,
                "product": prod["id"], "amount": amt,
                "price": prod["price"] * amt, "time": time.time() * 1000,
            })

    # ── Member CRUD ──
    elif cmd == "member_add":
        raw = msg["member"]
        pwd = raw.pop("password", "")
        m = {**raw, "id": str(uuid.uuid4()),
             "joined": datetime.now().strftime("%Y-%m-%d"), "flags": 0,
             "active": True,
             "password": hashlib.sha256(pwd.encode()).hexdigest() if pwd else ""}
        store.members.append(m)
    elif cmd == "member_edit":
        raw = msg["member"]
        if "password" in raw and raw["password"] and not raw["password"].startswith("sha256:"):
            raw["password"] = hashlib.sha256(raw["password"].encode()).hexdigest()
        store.members = [m if m["id"] != raw["id"] else {**m, **raw} for m in store.members]
    elif cmd == "member_del":
        store.members = [m for m in store.members if m["id"] != msg["id"]]
    elif cmd == "member_topup":
        for m in store.members:
            if m["id"] == msg["memberId"]:
                m["credit"] = round(m["credit"] + msg["amount"], 2)

    # ── Employee CRUD ──
    elif cmd == "employee_add":
        e = {**msg["employee"], "id": str(uuid.uuid4()), "active": True,
             "password": hashlib.sha256(msg["employee"]["password"].encode()).hexdigest()}
        store.employees.append(e)
    elif cmd == "employee_del":
        store.employees = [e for e in store.employees if e["id"] != msg["id"]]

    # ── Expense ──
    elif cmd == "expense_add":
        store.expenses.insert(0, {**msg["expense"], "id": str(uuid.uuid4()), "time": time.time() * 1000})

    # ── Settings ──
    elif cmd == "settings_save":
        store.settings.update(msg["settings"])

    # ── Login ──
    elif cmd == "login":
        emp = store.validate_employee(msg.get("username",""), msg.get("password",""))
        await ws.send(json.dumps({
            "type": "login_result",
            "ok": emp is not None,
            "employee": {k:v for k,v in emp.items() if k != "password"} if emp else None,
        }))
        return

    else:
        reply["ok"] = False
        reply["error"] = f"Unknown command: {cmd}"

    # Broadcast updated state to all UI clients
    await broadcast_ui()
    await ws.send(json.dumps(reply))

# ─── Periodic state push ─────────────────────────────────────────────────────
async def ticker():
    """Push live state to UI every TICK_SECS seconds."""
    while True:
        await asyncio.sleep(TICK_SECS)
        for pc_id, pc in list(store.clients.items()):
            # Mark disconnected PCs (no heartbeat in 15s)
            if pc["connected"] and (time.time() - pc.get("lastSeen", 0)) > 15:
                pc["connected"] = False

            # Auto-lock + stop when session timeout expires
            if pc["status"] == "active" and pc.get("timeout") and pc.get("startTime"):
                time_left = _time_left(pc)
                if time_left == 0:
                    log.info(f"Session timeout expired: {pc_id} — locking and stopping")
                    # Lock the screen first
                    await send_to_agent(pc_id, {"type": "lock"})
                    # Stop the session and log it
                    session = store.stop_session(pc_id)
                    # Notify the agent
                    await send_to_agent(pc_id, {"type": "stop", "session": session})

        await broadcast_ui()

async def db_saver():
    """Periodically flush store to SQLite every 10 seconds."""
    while True:
        await asyncio.sleep(10)
        save_store(store)

# ─── Entry point ─────────────────────────────────────────────────────────────
async def main():
    log.info("=" * 50)
    log.info("  Nordseye Cyber Cafe Server")
    log.info("=" * 50)
    log.info(f"  Looking for: {_INDEX_HTML}")
    if not _INDEX_HTML.exists():
        log.error(f"index.html not found at {_INDEX_HTML}")
        log.error("Place index.html in the same folder as server.py")
        sys.exit(1)
    log.info(f"  Agent port : {AGENT_PORT}  (client PCs)")
    log.info(f"  UI port    : {UI_PORT}     (browser dashboard)")
    import socket as _socket
    try:
        _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        _local_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        _local_ip = "localhost"
    log.info(f"  Open browser: http://{_local_ip}:{UI_PORT}/")
    log.info(f"  Local only  : http://localhost:{UI_PORT}/")
    log.info("  Default login: admin / 1234")
    log.info("=" * 50)

    agent_server = await websockets.serve(
        handle_agent, "0.0.0.0", AGENT_PORT,
        reuse_address=True, reuse_port=True,
    )
    ui_server    = await websockets.serve(
        handle_ui, "0.0.0.0", UI_PORT,
        process_request=process_ui_request,
        reuse_address=True, reuse_port=True,
    )

    await asyncio.gather(
        agent_server.wait_closed(),
        ui_server.wait_closed(),
        ticker(),
        db_saver(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped.")
