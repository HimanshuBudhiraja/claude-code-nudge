#!/usr/bin/env python3
"""
Claude Code Nudge Daemon — Chat UI
Floating chat widget that intercepts Claude Code hooks.
"""

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = 9981
CHAT_W = 380
CHAT_H = 480
SCREEN_MARGIN_RIGHT = 20
SCREEN_MARGIN_BOTTOM = 80
ASK_TIMEOUT_SECONDS = 120
PID_FILE = os.path.expanduser("~/.claude/nudge/daemon.pid")

# — Colors: dark chat theme —
BG_WINDOW     = "#0D1117"   # GitHub-dark base
BG_HEADER     = "#161B22"   # slightly lighter header
BG_CHAT       = "#0D1117"   # chat scroll area
BG_INPUT      = "#161B22"   # input bar
BUBBLE_CLAUDE = "#1F2937"   # Claude bubbles (dark slate)
BUBBLE_USER   = "#1D4ED8"   # user bubbles (blue)
BUBBLE_SYS    = "#14532D"   # system/notify (dark green)
FG            = "#E6EDF3"
FG_DIM        = "#8B949E"
FG_BUBBLE     = "#E6EDF3"
ACCENT        = "#58A6FF"   # blue links / title
BTN_OPT_BG   = "#21262D"
BTN_OPT_HOVER = "#30363D"
BTN_OPT_FG   = "#58A6FF"
BTN_SEND_BG  = "#1F6FEB"
BTN_SEND_HOV = "#388BFD"
BORDER        = "#30363D"
DOT_ONLINE    = "#3FB950"

# ---------------------------------------------------------------------------
# Shared state bus
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_gui_command_queue: queue.Queue = queue.Queue(maxsize=8)
_response_queue: queue.Queue = queue.Queue(maxsize=1)

# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class NudgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "running"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return

        if self.path == "/ask":
            self._handle_ask(body)
        elif self.path == "/stop":
            self._handle_stop(body)
        elif self.path == "/notify":
            self._handle_notify(body)
        else:
            self._json(404, {"error": "not found"})

    def _handle_ask(self, body):
        question = body.get("question", "Claude is asking…")
        options  = body.get("options", ["OK"])

        with _state_lock:
            while not _response_queue.empty():
                try:
                    _response_queue.get_nowait()
                except queue.Empty:
                    break

            try:
                _gui_command_queue.put_nowait({
                    "mode": "ask",
                    "question": question,
                    "options": options,
                })
            except queue.Full:
                pass

            threading.Thread(target=_play_notification, daemon=True).start()

            try:
                chosen = _response_queue.get(timeout=ASK_TIMEOUT_SECONDS)
            except queue.Empty:
                chosen = "__timeout__"

        if chosen == "__timeout__":
            result = {"decision": "block", "reason": "No response from user (timeout)"}
        elif chosen == "__cancel__":
            result = {"decision": "block", "reason": "User dismissed the question"}
        else:
            result = {"decision": "block", "reason": f"User selected: {chosen}"}

        self._json(200, result)

    def _handle_stop(self, body):
        self._json(200, {"status": "ok"})
        try:
            _gui_command_queue.put_nowait({"mode": "stop"})
        except queue.Full:
            pass
        threading.Thread(target=_play_notification, daemon=True).start()

    def _handle_notify(self, body):
        message = body.get("message", "Claude notification")
        self._json(200, {"status": "ok"})
        try:
            _gui_command_queue.put_nowait({"mode": "notify", "message": message})
        except queue.Full:
            pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _play_notification():
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "Claude needs your input!" '
             'with title "Claude Code" sound name "Glass"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def _run_http_server():
    try:
        server = _ReusableHTTPServer(("localhost", PORT), NudgeHandler)
        server.serve_forever()
    except OSError as e:
        print(f"[nudge] HTTP server failed: {e}", file=sys.stderr)
        os._exit(1)


# ---------------------------------------------------------------------------
# AppleScript text injection
# ---------------------------------------------------------------------------

def _inject_text_to_vscode(text: str):
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Visual Studio Code" to activate\n'
        'delay 0.4\n'
        'tell application "System Events"\n'
        f'    keystroke "{escaped}"\n'
        '    key code 36\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"[nudge] AppleScript error: {result.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[nudge] AppleScript exception: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Chat UI
# ---------------------------------------------------------------------------

class ChatApp:
    """
    Persistent chat window — hidden when idle, slides in when Claude needs input.
    Messages accumulate as bubbles (Claude left, user right).
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self._pending_options = []    # current AskUserQuestion options
        self._input_mode = None       # "stop" or None

        # Screen dimensions
        root.update_idletasks()
        self._sw = root.winfo_screenwidth()
        self._sh = root.winfo_screenheight()

        # Window chrome
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.97)
        root.configure(bg=BORDER)
        root.resizable(False, False)

        self._build_ui()
        root.withdraw()
        root.after(50, self._poll_commands)

    # -----------------------------------------------------------------------
    # Build UI
    # -----------------------------------------------------------------------

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG_WINDOW, padx=0, pady=0)
        outer.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self._build_header(outer)
        self._build_chat_area(outer)
        self._build_input_area(outer)

    def _build_header(self, parent):
        hdr = tk.Frame(parent, bg=BG_HEADER, height=44)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        # Drag support
        hdr.bind("<Button-1>", self._drag_start)
        hdr.bind("<B1-Motion>", self._drag_move)

        # Avatar circle "C"
        av = tk.Label(hdr, text="C", bg=ACCENT, fg="#FFFFFF",
                      font=("Helvetica Neue", 11, "bold"),
                      width=2, height=1)
        av.pack(side=tk.LEFT, padx=(12, 8), pady=10)

        # Title + subtitle
        title_col = tk.Frame(hdr, bg=BG_HEADER)
        title_col.pack(side=tk.LEFT)
        tk.Label(title_col, text="Claude Code", bg=BG_HEADER, fg=FG,
                 font=("Helvetica Neue", 12, "bold"), anchor="w").pack(anchor="w")
        self._status_lbl = tk.Label(title_col, text="● Active", bg=BG_HEADER,
                                    fg=DOT_ONLINE, font=("Helvetica Neue", 9), anchor="w")
        self._status_lbl.pack(anchor="w")

        # Close button
        tk.Button(hdr, text="✕", bg=BG_HEADER, fg=FG_DIM, relief=tk.FLAT,
                  bd=0, padx=10, pady=4, cursor="hand2",
                  font=("Helvetica Neue", 12),
                  activebackground=BG_HEADER, activeforeground=FG,
                  command=self._hide_window).pack(side=tk.RIGHT, padx=4)

    def _build_chat_area(self, parent):
        chat_frame = tk.Frame(parent, bg=BG_CHAT)
        chat_frame.pack(fill=tk.BOTH, expand=True)

        # Scrollable canvas
        self._canvas = tk.Canvas(chat_frame, bg=BG_CHAT, highlightthickness=0,
                                  bd=0)
        self._scrollbar = tk.Scrollbar(chat_frame, orient="vertical",
                                        command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Inner frame for messages
        self._msg_frame = tk.Frame(self._canvas, bg=BG_CHAT)
        self._canvas_win = self._canvas.create_window(
            (0, 0), window=self._msg_frame, anchor="nw"
        )

        self._msg_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Mouse wheel scroll
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._msg_frame.bind("<MouseWheel>", self._on_mousewheel)

    def _build_input_area(self, parent):
        self._input_bar = tk.Frame(parent, bg=BG_INPUT, pady=8)
        self._input_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._entry = tk.Text(
            self._input_bar, bg=BG_INPUT, fg=FG,
            insertbackground=FG,
            relief=tk.FLAT, bd=0,
            height=2, wrap=tk.WORD,
            font=("Helvetica Neue", 12),
            padx=8, pady=4,
        )
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 6))
        self._entry.insert("1.0", "Type a message…")
        self._entry.configure(fg=FG_DIM)
        self._entry.bind("<FocusIn>", self._on_entry_focus_in)
        self._entry.bind("<FocusOut>", self._on_entry_focus_out)
        self._entry.bind("<Command-Return>", lambda e: self._on_send())
        self._entry.bind("<Control-Return>", lambda e: self._on_send())

        send_btn = tk.Button(
            self._input_bar, text="▶",
            bg=BTN_SEND_BG, fg="#FFFFFF",
            relief=tk.FLAT, bd=0,
            padx=10, pady=6,
            cursor="hand2",
            font=("Helvetica Neue", 13, "bold"),
            activebackground=BTN_SEND_HOV, activeforeground="#FFFFFF",
            command=self._on_send,
        )
        send_btn.pack(side=tk.RIGHT, padx=(0, 10))

        # Input bar is always visible (but entry is disabled when not in stop mode)
        self._set_input_active(False)

    # -----------------------------------------------------------------------
    # Drag to reposition
    # -----------------------------------------------------------------------

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # -----------------------------------------------------------------------
    # Scroll helpers
    # -----------------------------------------------------------------------

    def _on_frame_configure(self, event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_win, width=event.width)

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _scroll_to_bottom(self):
        self.root.update_idletasks()
        self._canvas.yview_moveto(1.0)

    # -----------------------------------------------------------------------
    # Entry placeholder
    # -----------------------------------------------------------------------

    def _on_entry_focus_in(self, event):
        if self._entry.get("1.0", tk.END).strip() == "Type a message…":
            self._entry.delete("1.0", tk.END)
            self._entry.configure(fg=FG)

    def _on_entry_focus_out(self, event):
        if not self._entry.get("1.0", tk.END).strip():
            self._entry.insert("1.0", "Type a message…")
            self._entry.configure(fg=FG_DIM)

    def _set_input_active(self, active: bool):
        state = tk.NORMAL if active else tk.DISABLED
        self._entry.configure(state=state)
        if not active:
            self._entry.configure(fg=FG_DIM)

    # -----------------------------------------------------------------------
    # Add messages to chat
    # -----------------------------------------------------------------------

    def _add_claude_message(self, text: str):
        """Left-aligned message bubble from Claude."""
        row = tk.Frame(self._msg_frame, bg=BG_CHAT)
        row.pack(fill=tk.X, padx=10, pady=4, anchor="w")

        # Avatar
        tk.Label(row, text="C", bg=ACCENT, fg="#FFF",
                 font=("Helvetica Neue", 9, "bold"),
                 width=2).pack(side=tk.LEFT, anchor="n", padx=(0, 6))

        bubble = tk.Frame(row, bg=BUBBLE_CLAUDE)
        bubble.pack(side=tk.LEFT, anchor="w")

        tk.Label(bubble, text=text, bg=BUBBLE_CLAUDE, fg=FG_BUBBLE,
                 wraplength=CHAT_W - 100,
                 justify=tk.LEFT, anchor="w",
                 font=("Helvetica Neue", 12),
                 padx=12, pady=8).pack()

        self._scroll_to_bottom()

    def _add_user_message(self, text: str):
        """Right-aligned message bubble from user."""
        row = tk.Frame(self._msg_frame, bg=BG_CHAT)
        row.pack(fill=tk.X, padx=10, pady=4, anchor="e")

        bubble = tk.Frame(row, bg=BUBBLE_USER)
        bubble.pack(side=tk.RIGHT, anchor="e")

        tk.Label(bubble, text=text, bg=BUBBLE_USER, fg="#FFFFFF",
                 wraplength=CHAT_W - 100,
                 justify=tk.LEFT, anchor="w",
                 font=("Helvetica Neue", 12),
                 padx=12, pady=8).pack()

        self._scroll_to_bottom()

    def _add_system_message(self, text: str):
        """Centered system / notification message."""
        row = tk.Frame(self._msg_frame, bg=BG_CHAT)
        row.pack(fill=tk.X, padx=20, pady=6)

        tk.Label(row, text=text, bg=BUBBLE_SYS, fg="#86EFAC",
                 wraplength=CHAT_W - 60,
                 justify=tk.CENTER, anchor="center",
                 font=("Helvetica Neue", 11),
                 padx=10, pady=6).pack(fill=tk.X)

        self._scroll_to_bottom()

    def _add_option_buttons(self, options: list):
        """Quick-reply option buttons below the last Claude message."""
        self._clear_options()

        self._options_frame = tk.Frame(self._msg_frame, bg=BG_CHAT)
        self._options_frame.pack(fill=tk.X, padx=46, pady=(0, 6), anchor="w")

        for label in options:
            btn = tk.Button(
                self._options_frame,
                text=label,
                bg=BTN_OPT_BG, fg=BTN_OPT_FG,
                relief=tk.FLAT, bd=0,
                padx=12, pady=6,
                cursor="hand2",
                font=("Helvetica Neue", 11),
                activebackground=BTN_OPT_HOVER, activeforeground=BTN_OPT_FG,
                command=lambda l=label: self._on_option_selected(l),
            )
            btn.pack(fill=tk.X, pady=2)

        self._scroll_to_bottom()

    def _clear_options(self):
        if hasattr(self, "_options_frame") and self._options_frame.winfo_exists():
            self._options_frame.destroy()

    # -----------------------------------------------------------------------
    # Command polling (main-thread safe)
    # -----------------------------------------------------------------------

    def _poll_commands(self):
        try:
            cmd = _gui_command_queue.get_nowait()
            mode = cmd.get("mode")
            if mode == "ask":
                self._handle_ask_mode(cmd["question"], cmd["options"])
            elif mode == "stop":
                self._handle_stop_mode()
            elif mode == "notify":
                self._handle_notify_mode(cmd.get("message", ""))
        except queue.Empty:
            pass
        self.root.after(50, self._poll_commands)

    # -----------------------------------------------------------------------
    # Modes
    # -----------------------------------------------------------------------

    def _handle_ask_mode(self, question: str, options: list):
        self._pending_options = options
        self._input_mode = None
        self._set_input_active(False)
        self._add_claude_message(question)
        self._add_option_buttons(options)
        self._show_window()

    def _handle_stop_mode(self):
        self._input_mode = "stop"
        self._clear_options()
        self._add_claude_message("I've finished. What would you like to do next?")
        self._set_input_active(True)
        self._show_window()
        self.root.after(100, lambda: (
            self.root.focus_force(),
            self._entry.focus_set()
        ))

    def _handle_notify_mode(self, message: str):
        self._add_system_message(message)
        self._show_window()
        # Auto-hide after 5s if no pending interaction
        if self._input_mode is None and not self._pending_options:
            self.root.after(5000, self._maybe_hide)

    def _maybe_hide(self):
        """Hide only if no pending interaction is waiting."""
        if self._input_mode is None and not self._pending_options:
            self._hide_window()

    # -----------------------------------------------------------------------
    # User actions
    # -----------------------------------------------------------------------

    def _on_option_selected(self, label: str):
        self._clear_options()
        self._pending_options = []
        self._add_user_message(label)
        self._hide_window()
        try:
            _response_queue.put_nowait(label)
        except queue.Full:
            pass

    def _on_send(self):
        text = self._entry.get("1.0", tk.END).strip()
        if not text or text == "Type a message…":
            return
        self._entry.delete("1.0", tk.END)
        self._entry.insert("1.0", "Type a message…")
        self._entry.configure(fg=FG_DIM)
        self._input_mode = None
        self._set_input_active(False)
        self._add_user_message(text)
        self._hide_window()
        threading.Thread(
            target=_inject_text_to_vscode, args=(text,), daemon=True
        ).start()

    # -----------------------------------------------------------------------
    # Window management
    # -----------------------------------------------------------------------

    def _show_window(self):
        x = self._sw - CHAT_W - SCREEN_MARGIN_RIGHT
        y = self._sh - CHAT_H - SCREEN_MARGIN_BOTTOM
        self.root.geometry(f"{CHAT_W}x{CHAT_H}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self._update_status("● Active")

    def _hide_window(self):
        self.root.withdraw()

    def _update_status(self, text: str, color: str = DOT_ONLINE):
        self._status_lbl.configure(text=text, fg=color)

    # -----------------------------------------------------------------------
    # Cancel / close
    # -----------------------------------------------------------------------

    def _on_cancel(self):
        self._clear_options()
        self._pending_options = []
        self._input_mode = None
        self._hide_window()
        try:
            _response_queue.put_nowait("__cancel__")
        except queue.Full:
            pass


# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------

def _write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def _remove_pid():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass

def _on_signal(sig, frame):
    _remove_pid()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _write_pid()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()

    for _ in range(30):
        try:
            s = socket.create_connection(("localhost", PORT), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.1)

    root = tk.Tk()
    ChatApp(root)

    try:
        root.mainloop()
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
