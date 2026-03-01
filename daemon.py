#!/usr/bin/env python3
"""
Claude Code Nudge Daemon
Floating widget that intercepts Claude hooks and surfaces them as a macOS UI.
Runs as a persistent background process; hook scripts POST to it via curl.
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = 9981
WIDGET_W = 420
SCREEN_MARGIN_RIGHT = 20
SCREEN_MARGIN_BOTTOM = 80          # leaves room above the macOS Dock
ASK_TIMEOUT_SECONDS = 120
NOTIFY_AUTO_DISMISS_MS = 4000
PID_FILE = os.path.expanduser("~/.claude/nudge/daemon.pid")

# Colors — dark theme matching VS Code
BG        = "#2D2D2D"
FG        = "#ECECEC"
FG_DIM    = "#999999"
BTN_BG    = "#4A90D9"
BTN_FG    = "#FFFFFF"
BTN_SEND  = "#5CB85C"
BTN_CANCEL = "#E05252"
INPUT_BG  = "#3A3A3A"
BORDER    = "#555555"
TITLE_FG  = "#61AFEF"

# ---------------------------------------------------------------------------
# Shared state bus (GUI main thread <-> HTTP worker threads)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_gui_command_queue: queue.Queue = queue.Queue(maxsize=4)
_response_queue: queue.Queue = queue.Queue(maxsize=1)

# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class NudgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress request logs

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
            # Drain stale responses
            while not _response_queue.empty():
                try:
                    _response_queue.get_nowait()
                except queue.Empty:
                    break

            # Tell GUI to show question mode
            try:
                _gui_command_queue.put_nowait({
                    "mode": "ask",
                    "question": question,
                    "options": options,
                })
            except queue.Full:
                pass

            # Play notification sound
            threading.Thread(target=_play_notification, daemon=True).start()

            # Block until user clicks or timeout
            try:
                chosen = _response_queue.get(timeout=ASK_TIMEOUT_SECONDS)
            except queue.Empty:
                chosen = "__timeout__"

        if chosen == "__timeout__":
            result = {"decision": "block", "reason": "No response from user (timeout after 120s)"}
        elif chosen == "__cancel__":
            result = {"decision": "block", "reason": "User dismissed the question widget without selecting"}
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
        # Force exit so launchd KeepAlive can restart us cleanly
        os._exit(1)


# ---------------------------------------------------------------------------
# AppleScript text injection (Stop mode)
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
# Tkinter GUI
# ---------------------------------------------------------------------------

class NudgeApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self._notify_cancel_id = None

        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.95)

        root.update_idletasks()
        self._sw = root.winfo_screenwidth()
        self._sh = root.winfo_screenheight()

        root.configure(bg=BORDER)
        root.resizable(False, False)

        self.frame = tk.Frame(root, bg=BG, padx=14, pady=12)
        self.frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        root.withdraw()
        root.after(50, self._poll_commands)

    def _poll_commands(self):
        try:
            cmd = _gui_command_queue.get_nowait()
            mode = cmd.get("mode")
            if mode == "ask":
                self.show_ask(cmd["question"], cmd["options"])
            elif mode == "stop":
                self.show_stop()
            elif mode == "notify":
                self.show_notify(cmd.get("message", ""))
        except queue.Empty:
            pass
        self.root.after(50, self._poll_commands)

    def _position(self, w: int, h: int):
        x = self._sw - w - SCREEN_MARGIN_RIGHT
        y = self._sh - h - SCREEN_MARGIN_BOTTOM
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _clear_frame(self):
        if self._notify_cancel_id:
            self.root.after_cancel(self._notify_cancel_id)
            self._notify_cancel_id = None
        for widget in self.frame.winfo_children():
            widget.destroy()

    def _show_window(self, w: int, h: int):
        self._position(w, h)
        self.root.deiconify()
        self.root.lift()

    def _hide_window(self):
        self.root.withdraw()

    def _add_title(self, text: str):
        bar = tk.Frame(self.frame, bg=BG)
        bar.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            bar, text=text, bg=BG, fg=TITLE_FG,
            font=("Helvetica Neue", 12, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT)

        tk.Button(
            bar, text="✕", bg=BG, fg=FG_DIM, relief=tk.FLAT,
            bd=0, padx=4, cursor="hand2",
            command=self._on_cancel,
            font=("Helvetica Neue", 10),
            activebackground=BG, activeforeground=FG,
        ).pack(side=tk.RIGHT)

    def _on_cancel(self):
        self._hide_window()
        try:
            _response_queue.put_nowait("__cancel__")
        except queue.Full:
            pass

    # --- ask mode ----------------------------------------------------------

    def show_ask(self, question: str, options: list):
        self._clear_frame()
        self._add_title("Claude Code  ·  Question")

        tk.Label(
            self.frame, text=question, bg=BG, fg=FG,
            wraplength=WIDGET_W - 50, justify=tk.LEFT,
            font=("Helvetica Neue", 12),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 12))

        for label in options:
            btn = tk.Button(
                self.frame, text=label,
                bg=BTN_BG, fg=BTN_FG,
                relief=tk.FLAT, bd=0,
                padx=10, pady=7,
                cursor="hand2",
                font=("Helvetica Neue", 11),
                activebackground="#5BA3E8", activeforeground=BTN_FG,
                command=lambda l=label: self._on_option_selected(l),
            )
            btn.pack(fill=tk.X, pady=2)

        h = 100 + max(len(options) * 38, 38)
        self._show_window(WIDGET_W, h)

    def _on_option_selected(self, label: str):
        self._hide_window()
        try:
            _response_queue.put_nowait(label)
        except queue.Full:
            pass

    # --- stop mode ---------------------------------------------------------

    def show_stop(self):
        self._clear_frame()
        self._add_title("Claude Code  ·  Waiting for input")

        tk.Label(
            self.frame,
            text="Claude has finished and is waiting for your message.",
            bg=BG, fg=FG_DIM,
            wraplength=WIDGET_W - 50, justify=tk.LEFT,
            font=("Helvetica Neue", 11),
        ).pack(fill=tk.X, pady=(0, 10))

        self._stop_entry = tk.Text(
            self.frame, bg=INPUT_BG, fg=FG,
            insertbackground=FG,
            relief=tk.FLAT, bd=0,
            height=4, wrap=tk.WORD,
            font=("Helvetica Neue", 12),
            padx=8, pady=6,
        )
        self._stop_entry.pack(fill=tk.X, pady=(0, 10))

        btn_row = tk.Frame(self.frame, bg=BG)
        btn_row.pack(fill=tk.X)

        tk.Button(
            btn_row, text="Dismiss",
            bg=BTN_CANCEL, fg=BTN_FG,
            relief=tk.FLAT, bd=0, padx=10, pady=6,
            cursor="hand2",
            font=("Helvetica Neue", 11),
            activebackground="#E86060", activeforeground=BTN_FG,
            command=self._hide_window,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        tk.Button(
            btn_row, text="Send  ↵",
            bg=BTN_SEND, fg=BTN_FG,
            relief=tk.FLAT, bd=0, padx=10, pady=6,
            cursor="hand2",
            font=("Helvetica Neue", 11, "bold"),
            activebackground="#6CC86C", activeforeground=BTN_FG,
            command=self._on_stop_send,
        ).pack(side=tk.RIGHT)

        self._stop_entry.bind("<Command-Return>", lambda e: self._on_stop_send())
        self._stop_entry.bind("<Control-Return>", lambda e: self._on_stop_send())

        self._show_window(WIDGET_W, 230)
        self.root.focus_force()
        self._stop_entry.focus_set()

    def _on_stop_send(self):
        text = self._stop_entry.get("1.0", tk.END).strip()
        if not text:
            return
        self._hide_window()
        threading.Thread(
            target=_inject_text_to_vscode, args=(text,), daemon=True
        ).start()

    # --- notify mode -------------------------------------------------------

    def show_notify(self, message: str):
        self._clear_frame()
        self._add_title("Claude Code")

        tk.Label(
            self.frame, text=message,
            bg=BG, fg=FG,
            wraplength=WIDGET_W - 50, justify=tk.LEFT,
            font=("Helvetica Neue", 11),
        ).pack(fill=tk.X, pady=(0, 4))

        self._show_window(WIDGET_W, 90)
        self._notify_cancel_id = self.root.after(
            NOTIFY_AUTO_DISMISS_MS, self._hide_window
        )


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

    # Wait for server to be ready
    for _ in range(30):
        try:
            s = socket.create_connection(("localhost", PORT), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.1)

    root = tk.Tk()
    NudgeApp(root)

    try:
        root.mainloop()
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
