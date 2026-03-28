#!/usr/bin/env python3
"""
Nordseye Client Agent
---------------------
Runs on each customer PC. Connects to the server, shows a time-remaining
overlay, and locks/unlocks the screen on command.

Usage:
  python agent.py --server 192.168.1.122.1 --name "PC 01"

Requirements:
  pip install websockets

Linux extras:
  pip install pillow  (optional)
"""

import asyncio
import json
import platform
import socket
import sys
import time
import threading
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("agent")

try:
    import websockets
except ImportError:
    print("[ERROR] Run: pip install websockets")
    sys.exit(1)

OS = platform.system()

# ─── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Nordseye Client Agent")
parser.add_argument("--server",  default="127.0.0.1")
parser.add_argument("--port",    default=8765, type=int)
parser.add_argument("--name",    default=socket.gethostname())
parser.add_argument("--id",      default=None)
args = parser.parse_args()

PC_ID   = args.id or socket.gethostname().lower().replace(" ", "_")
PC_NAME = args.name
SERVER  = f"ws://{args.server}:{args.port}"
RECONNECT_DELAY = 5

# ─── Shared state ─────────────────────────────────────────────────────────────
_overlay_status       = "inactive"
_overlay_owed         = 0.0
_overlay_time_left    = None
_lock_active          = False
_currency             = "\u20b1"
_login_mode           = "both"
_server_start_time    = None
_local_sync_time      = 0.0
_to_server_queue      = []
_tariff               = 0.0
_overlay_error_msg    = ""
_overlay_session_summary = None
_connected            = False

# ─── Palette ──────────────────────────────────────────────────────────────────
C = {
    "bg":      "#0d0f14",
    "surface": "#141720",
    "surf2":   "#1c2030",
    "surf3":   "#232840",
    "border":  "#2a3050",
    "accent":  "#4f8ef7",
    "accent2": "#7c5cfc",
    "green":   "#22c55e",
    "amber":   "#f59e0b",
    "red":     "#ef4444",
    "text":    "#e8eaf0",
    "text2":   "#9aa3b8",
    "text3":   "#5c637a",
}

def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _blend(h1, h2, t):
    r1,g1,b1 = _hex_to_rgb(h1)
    r2,g2,b2 = _hex_to_rgb(h2)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t))


# ─── Overlay ──────────────────────────────────────────────────────────────────
def _run_overlay_tk():
    global _overlay_status, _overlay_owed, _overlay_time_left
    global _lock_active, _currency, _server_start_time, _local_sync_time
    global _to_server_queue, _login_mode, _tariff

    try:
        import tkinter as tk
    except ImportError:
        log.warning("tkinter not available — no overlay")
        return

    root = tk.Tk()
    root.title("Nordseye")
    root.attributes("-topmost", True)
    root.overrideredirect(True)

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    W_COL, H_COL = 210, 32
    W_EXP, H_EXP = 330, 210
    _collapsed = [True]

    def _geom():
        ox = 260
        if _collapsed[0]:
            root.geometry(f"{W_COL}x{H_COL}+{sw//2 - W_COL//2 + ox}+0")
        else:
            root.geometry(f"{W_EXP}x{H_EXP}+{sw//2 - W_EXP//2 + ox}+0")

    _geom()
    root.configure(bg=C["bg"])

    # ── Rounded entry widget ───────────────────────────────────────────────────
    class RoundedEntry(tk.Frame):
        def __init__(self, parent, width_chars=12, justify="center",
                     var=None, show="", fg=C["text"], accent=C["accent"]):
            super().__init__(parent, bg=C["surface"])
            self._accent = accent
            cw = max(width_chars * 15, 150)
            h  = 40
            c  = tk.Canvas(self, width=cw, height=h,
                           bg=C["surface"], highlightthickness=0)
            c.pack()
            r = h // 2
            # pill shape
            c.create_oval(0, 0, 2*r, h, fill=C["surf3"], outline=C["border"])
            c.create_oval(cw-2*r, 0, cw, h, fill=C["surf3"], outline=C["border"])
            c.create_rectangle(r, 0, cw-r, h, fill=C["surf3"], outline=C["surf3"])
            c.bind("<Button-1>", lambda e: self._e.focus())
            self._e = tk.Entry(
                self, textvariable=var,
                font=("Monospace", 13, "bold") if not show else ("Sans", 12),
                justify=justify, bg=C["surf3"], fg=fg,
                insertbackground=C["text"], relief="flat",
                show=show, highlightthickness=0, bd=0)
            self._e.place(relx=0.5, rely=0.5, anchor="center",
                          width=cw-28, height=h-10)
            self._e.bind("<FocusIn>",  lambda e: c.config(highlightbackground=accent, highlightthickness=2))
            self._e.bind("<FocusOut>", lambda e: c.config(highlightthickness=0))
        def bind(self, s, f): self._e.bind(s, f)
        def focus(self):      self._e.focus()
        def get(self):        return self._e.get()

    # ── Button helper ─────────────────────────────────────────────────────────
    def _btn(parent, text, color, fg="#fff", cmd=None):
        f = tk.Frame(parent, bg=color, cursor="hand2")
        l = tk.Label(f, text=text, font=("Sans", 10, "bold"),
                     bg=color, fg=fg, padx=14, pady=9)
        l.pack(fill="both", expand=True)
        li = _blend(color, "#fff", 0.15)
        def _e(e=None): f.config(bg=li); l.config(bg=li)
        def _o(e=None): f.config(bg=color); l.config(bg=color)
        def _c(e=None): _o(); cmd and cmd()
        for w in (f, l):
            w.bind("<Enter>", _e); w.bind("<Leave>", _o)
            w.bind("<Button-1>", _c)
        return f

    # ── Lock screen ───────────────────────────────────────────────────────────
    lock_win = [None]

    def show_lock():
        if lock_win[0]:
            return
        lw = tk.Toplevel(root)
        lock_win[0] = lw
        lw.geometry(f"{sw}x{sh}+0+0")
        lw.attributes("-fullscreen", True)
        lw.attributes("-topmost", True)
        lw.configure(bg=C["bg"])
        lw.protocol("WM_DELETE_WINDOW", lambda: None)
        lw.bind("<Escape>",  lambda e: "break")
        lw.bind("<Alt-F4>",  lambda e: "break")
        lw.bind("<Alt-Tab>", lambda e: "break")

        def _stay_top():
            if lw.winfo_exists():
                lw.lift(); lw.attributes("-topmost", True)
                lw.after(500, _stay_top)
        _stay_top()

        def _grab():
            try:
                if lw.winfo_exists(): lw.grab_set()
            except Exception as ex:
                log.warning(f"Grab failed: {ex}")
        lw.after(200, _grab)
        lw.focus_force()

        # Background grid
        bg = tk.Canvas(lw, width=sw, height=sh, bg=C["bg"],
                       highlightthickness=0)
        bg.place(x=0, y=0, relwidth=1, relheight=1)
        for x in range(0, sw, 52):
            bg.create_line(x, 0, x, sh, fill="#12161f", width=1)
        for y in range(0, sh, 52):
            bg.create_line(0, y, sw, y, fill="#12161f", width=1)

        # Top accent bar
        tk.Frame(lw, bg=C["accent"], height=3).place(x=0, y=0, relwidth=1)

        # Brand
        bf = tk.Frame(lw, bg=C["bg"])
        bf.place(relx=0.5, rely=0.14, anchor="center")
        tk.Label(bf, text="NORD", font=("Monospace", 44, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(side="left")
        tk.Label(bf, text="SEYE", font=("Monospace", 44, "bold"),
                 bg=C["bg"], fg=C["accent"]).pack(side="left")

        tk.Label(lw, text="TERMINAL  LOCKED",
                 font=("Sans", 10, "bold"),
                 bg=C["bg"], fg=C["text3"]).place(relx=0.5, rely=0.222, anchor="center")

        # Thin divider
        div = tk.Canvas(lw, width=280, height=1, bg=C["border"], highlightthickness=0)
        div.place(relx=0.5, rely=0.265, anchor="center")

        # Due / status label
        due_lbl = tk.Label(lw, text="", font=("Monospace", 18, "bold"),
                           bg=C["bg"], fg=C["amber"])
        due_lbl.place(relx=0.5, rely=0.31, anchor="center")
        lw.due_lbl = due_lbl

        # Login area
        login_frame = tk.Frame(lw, bg=C["bg"])
        login_frame.place(relx=0.5, rely=0.64, anchor="center")
        lw.login_frame = login_frame

        # Summary area
        sum_frame = tk.Frame(lw, bg=C["surface"],
                             highlightbackground=C["border"],
                             highlightthickness=1, padx=26, pady=20)
        lw.summary_frame = sum_frame

        # Error label
        err_lbl = tk.Label(lw, text="", font=("Sans", 11, "bold"),
                           bg=C["bg"], fg=C["red"])
        err_lbl.place(relx=0.5, rely=0.89, anchor="center")
        lw.err_lbl = err_lbl

        # Clock (bottom right)
        clk = tk.Label(lw, text="", font=("Monospace", 12),
                       bg=C["bg"], fg=C["text3"])
        clk.place(relx=0.97, rely=0.97, anchor="se")
        def _clk():
            if lw.winfo_exists():
                clk.config(text=time.strftime("%A  %d %b  %I:%M:%S %p"))
                lw.after(1000, _clk)
        _clk()

        # PC label (bottom left)
        tk.Label(lw, text=PC_NAME, font=("Sans", 10),
                 bg=C["bg"], fg=C["text3"]).place(relx=0.03, rely=0.97, anchor="sw")

        # ── Build login panels ─────────────────────────────────────────────────
        def _ticket_ui(parent):
            cf = tk.Frame(parent, bg=C["surface"],
                          highlightbackground=C["accent"],
                          highlightthickness=1, padx=26, pady=22)
            tk.Label(cf, text="TICKET CODE", font=("Sans", 9, "bold"),
                     bg=C["surface"], fg=C["accent"]).pack(pady=(0, 10))
            cv = tk.StringVar()
            e  = RoundedEntry(cf, width_chars=11, justify="center",
                              var=cv, fg=C["text"], accent=C["accent"])
            e.pack(pady=(0, 10))
            def _go(ev=None):
                code = cv.get().strip().upper()
                if code:
                    _to_server_queue.append({"type": "ticket_login", "code": code})
            e.bind("<Return>", _go)
            _btn(cf, "\u25b6  START SESSION", C["accent"], cmd=_go).pack(fill="x")
            return cf, e

        def _member_ui(parent):
            cf = tk.Frame(parent, bg=C["surface"],
                          highlightbackground=C["green"],
                          highlightthickness=1, padx=26, pady=22)
            tk.Label(cf, text="MEMBER LOGIN", font=("Sans", 9, "bold"),
                     bg=C["surface"], fg=C["green"]).pack(pady=(0, 10))
            tk.Label(cf, text="USERNAME", font=("Sans", 8),
                     bg=C["surface"], fg=C["text3"]).pack(anchor="w")
            uv = tk.StringVar()
            ue = RoundedEntry(cf, width_chars=13, var=uv,
                              fg=C["text"], accent=C["green"])
            ue.pack(pady=(2, 6))
            tk.Label(cf, text="PASSWORD", font=("Sans", 8),
                     bg=C["surface"], fg=C["text3"]).pack(anchor="w")
            pv = tk.StringVar()
            pe = RoundedEntry(cf, width_chars=13, var=pv,
                              fg=C["text"], accent=C["green"], show="\u25cf")
            pe.pack(pady=(2, 10))
            def _go(ev=None):
                u, p = uv.get().strip(), pv.get()
                if u and p:
                    _to_server_queue.append({"type": "member_login",
                                             "username": u, "password": p})
            ue.bind("<Return>", lambda e: pe.focus())
            pe.bind("<Return>", _go)
            _btn(cf, "\u2192  LOGIN", C["green"], cmd=_go).pack(fill="x")
            return cf, ue

        global _login_mode
        if _login_mode == "ticket":
            ui, foc = _ticket_ui(login_frame)
            ui.pack(); foc.focus()
        elif _login_mode == "member":
            ui, foc = _member_ui(login_frame)
            ui.pack(); foc.focus()
        else:
            row = tk.Frame(login_frame, bg=C["bg"])
            row.pack()
            t_ui, t_foc = _ticket_ui(row)
            t_ui.pack(side="left")
            mid = tk.Frame(row, bg=C["bg"], width=40)
            mid.pack(side="left", fill="y")
            mid.pack_propagate(False)
            tk.Label(mid, text="OR", font=("Sans", 9, "bold"),
                     bg=C["bg"], fg=C["text3"]).place(relx=0.5, rely=0.5, anchor="center")
            tk.Frame(mid, bg=C["border"], width=1, height=50).place(relx=0.5, rely=0.15, anchor="n")
            tk.Frame(mid, bg=C["border"], width=1, height=50).place(relx=0.5, rely=0.85, anchor="s")
            m_ui, m_foc = _member_ui(row)
            m_ui.pack(side="left")
            t_foc.focus()

    def hide_lock():
        lw = lock_win[0]
        if lw:
            try:
                lw.grab_release()
                lw.destroy()
            except Exception:
                pass
            lock_win[0] = None

    # ── Top collapsed bar ──────────────────────────────────────────────────────
    main_frame = tk.Frame(root, bg=C["bg"])
    main_frame.pack(fill="both", expand=True)

    top_bar = tk.Frame(main_frame, bg=C["surface"], height=H_COL, cursor="hand2")
    top_bar.pack(fill="x")
    top_bar.pack_propagate(False)
    tk.Frame(top_bar, bg=C["accent"], width=3).pack(side="left", fill="y")

    col_dot  = tk.Label(top_bar, text="\u25cb", font=("Monospace", 11),
                        bg=C["surface"], fg=C["text3"])
    col_dot.pack(side="left", padx=(6, 4))

    col_time = tk.Label(top_bar, text="--:--", font=("Monospace", 12, "bold"),
                        bg=C["surface"], fg=C["text"])
    col_time.pack(side="left")

    col_clk  = tk.Label(top_bar, text="", font=("Sans", 10),
                        bg=C["surface"], fg=C["text3"])
    col_clk.pack(side="left", padx=(10, 0))

    col_owed = tk.Label(top_bar, text="", font=("Monospace", 11, "bold"),
                        bg=C["surface"], fg=C["green"])
    col_owed.pack(side="right", padx=10)

    exp_frame = tk.Frame(main_frame, bg=C["bg"])

    def _toggle(e=None):
        _collapsed[0] = not _collapsed[0]
        _geom()
        if _collapsed[0]:
            exp_frame.pack_forget()
        else:
            exp_frame.pack(fill="both", expand=True)

    for w in (top_bar, col_dot, col_time, col_owed, col_clk):
        w.bind("<Button-1>", _toggle)

    # Expanded info
    inf = tk.Frame(exp_frame, bg=C["bg"], padx=14, pady=8)
    inf.pack(fill="x")
    name_lbl   = tk.Label(inf, text=PC_NAME, font=("Sans", 10, "bold"),
                          bg=C["bg"], fg=C["text2"])
    name_lbl.grid(row=0, column=0, sticky="w")
    status_lbl = tk.Label(inf, text="INACTIVE", font=("Monospace", 9),
                          bg=C["bg"], fg=C["text3"])
    status_lbl.grid(row=1, column=0, sticky="w")
    owed_lbl   = tk.Label(inf, text="", font=("Monospace", 24, "bold"),
                          bg=C["bg"], fg=C["green"])
    owed_lbl.grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 0))
    inf.columnconfigure(1, weight=1)

    ppm_lbl  = tk.Label(exp_frame, text="", font=("Sans", 9),
                        bg=C["bg"], fg=C["text3"])
    ppm_lbl.pack(anchor="e", padx=14)
    time_lbl = tk.Label(exp_frame, text="", font=("Monospace", 10),
                        bg=C["bg"], fg=C["text3"])
    time_lbl.pack(anchor="w", padx=14)
    tk.Frame(exp_frame, bg=C["border"], height=1).pack(fill="x", padx=14, pady=6)

    bf = tk.Frame(exp_frame, bg=C["bg"], padx=14, pady=4)
    bf.pack(fill="x")

    def _pause(): _to_server_queue.append({"type": "client_action", "action": "pause"})
    def _stop():  _to_server_queue.append({"type": "client_action", "action": "stop"})

    pause_btn = _btn(bf, "\u23f8  Pause", C["amber"], fg="#111", cmd=_pause)
    stop_btn  = _btn(bf, "\u25a0  End Session", C["red"], cmd=_stop)

    SC = {"active": C["green"], "paused": C["amber"], "inactive": C["text3"]}

    def tick():
        st   = _overlay_status
        owed = _overlay_owed
        tl   = _overlay_time_left
        lw   = lock_win[0]

        col_clk.config(text=time.strftime("%I:%M %p"))
        color = SC.get(st, C["text3"])
        dot = "\u25cf" if st == "active" else ("\u23f8" if st == "paused" else "\u25cb")
        col_dot.config(text=dot, fg=color)
        status_lbl.config(text=st.upper(), fg=color)

        if st in ("active", "paused"):
            col_owed.config(text=f"{_currency}{owed:.2f}")
            owed_lbl.config(text=f"{_currency}{owed:.2f}", fg=C["green"])
            ppm_lbl.config(text=f"{_currency}{_tariff/60:.2f}/min" if _tariff > 0 else "")

            elapsed = int(max(0, time.time() - _server_start_time)) if _server_start_time else 0

            if tl is not None:
                rems = int(tl * 60 - (time.time() - _local_sync_time))
                rems = max(0, rems)
                h, rem = divmod(rems, 3600)
                m, s   = divmod(rem, 60)
                fmt = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                tc  = C["red"] if rems <= 60 else (C["amber"] if rems <= 300 else C["text"])
                time_lbl.config(text=f"\u23f1  {fmt} remaining", fg=tc)
                col_time.config(text=fmt, fg=tc)
            else:
                h, rem = divmod(elapsed, 3600)
                m, s   = divmod(rem, 60)
                fmt = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                time_lbl.config(text=f"\u23f1  {fmt} elapsed", fg=C["text3"])
                col_time.config(text=fmt, fg=C["text"])

            if not _lock_active:
                pause_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
                stop_btn.pack(side="right", expand=True, fill="x", padx=(4, 0))
            else:
                pause_btn.pack_forget(); stop_btn.pack_forget()
        else:
            col_owed.config(text="")
            owed_lbl.config(text="")
            ppm_lbl.config(text="")
            time_lbl.config(text="Waiting for session\u2026", fg=C["text3"])
            col_time.config(text="--:--", fg=C["text3"])
            pause_btn.pack_forget(); stop_btn.pack_forget()

        # Lock/unlock
        if _lock_active and not lw:
            show_lock()
        elif not _lock_active and lw:
            hide_lock()

        # Update lock screen
        lw = lock_win[0]
        if lw and hasattr(lw, "due_lbl"):
            if owed > 0:
                lw.due_lbl.config(text=f"Total Due: {_currency}{owed:.2f}")
                if (hasattr(lw, "login_frame") and
                        lw.login_frame.winfo_ismapped()):
                    lw.login_frame.place_forget()
                if (hasattr(lw, "summary_frame") and
                        not lw.summary_frame.winfo_ismapped()):
                    sf = lw.summary_frame
                    sf.place(relx=0.5, rely=0.63, anchor="center")
                    for w in sf.winfo_children(): w.destroy()
                    tk.Label(sf, text="SESSION RECEIPT",
                             font=("Sans", 10, "bold"),
                             bg=C["surface"], fg=C["text2"]).pack(pady=(0, 10))
                    def _row(lb, vl, col=C["text"], bold=False, _sf=sf):
                        fr = tk.Frame(_sf, bg=C["surface"]); fr.pack(fill="x", pady=3)
                        tk.Label(fr, text=lb, font=("Sans", 10),
                                 bg=C["surface"], fg=C["text3"]).pack(side="left", padx=(0, 16))
                        tk.Label(fr, text=vl,
                                 font=("Monospace", 10, "bold" if bold else "normal"),
                                 bg=C["surface"], fg=col).pack(side="right")
                    global _overlay_session_summary
                    sess = _overlay_session_summary or {}
                    _row("Duration:", f"{sess.get('duration', 0)} min")
                    pc_ = sum(p.get("price", 0)*p.get("amount", 1) for p in sess.get("products", []))
                    tc_ = sess.get("price", 0) - pc_
                    _row("Time Cost:", f"{_currency}{tc_:.2f}", C["accent"])
                    if pc_ > 0:
                        _row("Products:", f"{_currency}{pc_:.2f}", C["amber"])
                    tk.Frame(sf, bg=C["border"], height=1).pack(fill="x", pady=6)
                    _row("TOTAL:", f"{_currency}{sess.get('price', 0):.2f}", C["green"], True)
            else:
                if not _connected:
                    lw.due_lbl.config(text="Connecting to server\u2026")
                elif st == "inactive":
                    lw.due_lbl.config(text="Enter credentials to begin.")
                else:
                    lw.due_lbl.config(text="")
                if (hasattr(lw, "summary_frame") and
                        lw.summary_frame.winfo_ismapped()):
                    lw.summary_frame.place_forget()
                if (hasattr(lw, "login_frame") and
                        not lw.login_frame.winfo_ismapped()):
                    lw.login_frame.place(relx=0.5, rely=0.64, anchor="center")

            global _overlay_error_msg
            if _overlay_error_msg:
                lw.err_lbl.config(text=f"\u26a0  {_overlay_error_msg}")
                _overlay_error_msg = ""
                def _clear(w=lw):
                    if w and w.winfo_exists():
                        w.err_lbl.config(text="")
                lw.after(3000, _clear)

        root.after(1000, tick)

    tick()
    root.mainloop()


# ─── WebSocket loop ────────────────────────────────────────────────────────────
async def agent_loop():
    global _overlay_status, _overlay_owed, _overlay_time_left
    global _lock_active, _currency, _server_start_time, _local_sync_time
    global _login_mode, _connected, _tariff, _overlay_session_summary
    global _overlay_error_msg

    while True:
        try:
            log.info(f"Connecting to {SERVER} ...")
            async with websockets.connect(SERVER) as ws:
                log.info("Connected.")
                await ws.send(json.dumps({
                    "type": "register", "id": PC_ID, "name": PC_NAME,
                    "platform": OS, "ip": get_local_ip(),
                }))
                _connected = True
                if _overlay_status == "inactive":
                    _lock_active = True

                hb_task = asyncio.create_task(heartbeat(ws))
                tx_task = asyncio.create_task(tx_loop(ws))
                try:
                    async for raw in ws:
                        msg   = json.loads(raw)
                        mtype = msg.get("type")

                        if mtype in ("init", "tick"):
                            new_st = msg.get("status", "inactive")
                            new_tl = msg.get("timeLeft")
                            sync = (new_tl != _overlay_time_left or
                                    new_st != _overlay_status or
                                    new_st == "paused")
                            _overlay_status    = new_st
                            _overlay_owed      = msg.get("owed", 0.0)
                            _overlay_time_left = new_tl
                            _server_start_time = msg.get("startTime")
                            if sync:
                                _local_sync_time = time.time()
                            if "settings" in msg:
                                _currency   = msg["settings"].get("currency", "\u20b1")
                                _login_mode = msg["settings"].get("login_mode", "both")

                        elif mtype == "start":
                            _overlay_status = "active"
                            _lock_active    = False
                            # tariff field is a tariff ID string (e.g. "t1"), not a price.
                            # The hourly rate is sent separately; default 0 if absent.
                            try:
                                _tariff = float(msg.get("hourPrice", 0.0))
                            except (ValueError, TypeError):
                                _tariff = 0.0
                            log.info(f"Session started, tariff={msg.get('tariff')}")

                        elif mtype == "stop":
                            _overlay_status    = "inactive"
                            _overlay_time_left = None
                            _server_start_time = None
                            _lock_active       = True
                            session = msg.get("session")
                            _overlay_session_summary = session
                            _overlay_owed = session.get("price", 0.0) if session else 0.0
                            log.info("Session stopped.")

                        elif mtype == "ticket_result":
                            if not msg.get("ok"):
                                _overlay_error_msg = msg.get("err", "Invalid Ticket")

                        elif mtype == "member_login_result":
                            if not msg.get("ok"):
                                _overlay_error_msg = msg.get("err", "Login failed")

                        elif mtype == "pause":
                            _overlay_status = msg.get("status", "paused")
                            log.info(f"Session {_overlay_status}.")

                        elif mtype == "lock":
                            _lock_active = True
                            log.info("Screen LOCKED by server.")

                        elif mtype == "unlock":
                            _lock_active = False
                            log.info("Screen UNLOCKED by server.")

                        elif mtype == "message":
                            text = msg.get("text", "")
                            log.info(f"Message: {text}")
                            _show_message(text)
                finally:
                    hb_task.cancel()
                    tx_task.cancel()

        except (websockets.exceptions.ConnectionClosed,
                OSError, ConnectionRefusedError) as e:
            log.warning(f"Disconnected ({e}). Retrying in {RECONNECT_DELAY}s …")
            _connected = False
        await asyncio.sleep(RECONNECT_DELAY)


async def tx_loop(ws):
    global _to_server_queue
    while True:
        if _to_server_queue:
            msg = _to_server_queue.pop(0)
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                _to_server_queue.insert(0, msg)
        await asyncio.sleep(0.1)


async def heartbeat(ws):
    while True:
        await asyncio.sleep(5)
        try:
            await ws.send(json.dumps({"type": "heartbeat", "id": PC_ID}))
        except Exception:
            break


def _show_message(text: str):
    def _popup():
        try:
            import tkinter as tk
            r = tk.Tk()
            r.title("Message \u2014 Nordseye")
            r.attributes("-topmost", True)
            w, h = 340, 175
            sx, sy = r.winfo_screenwidth(), r.winfo_screenheight()
            r.geometry(f"{w}x{h}+{sx//2-w//2}+{sy//2-h//2}")
            r.configure(bg=C["bg"])
            tk.Frame(r, bg=C["accent2"], height=3).pack(fill="x")
            tk.Label(r, text="\U0001f4e2  MESSAGE",
                     font=("Sans", 11, "bold"),
                     bg=C["bg"], fg=C["accent2"]).pack(pady=(14, 8))
            tk.Label(r, text=text, font=("Sans", 12),
                     bg=C["bg"], fg=C["text"], wraplength=300).pack(expand=True, padx=20)
            _btn_f = tk.Frame(r, bg=C["accent2"], cursor="hand2")
            _btn_f.pack(pady=12)
            _lbl = tk.Label(_btn_f, text="OK", font=("Sans", 11, "bold"),
                            bg=C["accent2"], fg="white", padx=24, pady=8)
            _lbl.pack()
            _btn_f.bind("<Button-1>", lambda e: r.destroy())
            _lbl.bind("<Button-1>", lambda e: r.destroy())
            r.after(10000, r.destroy)
            r.mainloop()
        except Exception:
            print(f"\n[MSG] {text}\n")
    threading.Thread(target=_popup, daemon=True).start()


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ─── Entry point ─────────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info(f"  Nordseye Agent  |  {PC_NAME}  ({OS})")
    log.info(f"  PC ID  : {PC_ID}")
    log.info(f"  Server : {SERVER}")
    log.info("=" * 50)
    threading.Thread(target=_run_overlay_tk, daemon=True).start()
    try:
        asyncio.run(agent_loop())
    except KeyboardInterrupt:
        log.info("Agent stopped.")


if __name__ == "__main__":
    main()
