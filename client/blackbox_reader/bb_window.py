"""The Black Box window: a small dark window that lives in the background.

Ctrl+Shift+N (a global hotkey, can be changed in settings.json -> "hotkey") shows or hides it. It has no button in the taskbar and the X only hides it, so
the logger keeps running. It shows what the logger is doing, has "Stop logging" (pauses reading and sending) and "Check for updates", and a small
"Exit completely" for the rare case somebody really wants the program gone until the next start.

Windows only (tkinter + user32 through ctypes); on any other system `available()` is False and the reader just runs without a window.
Everything that is not drawing (hotkey parsing, status text) is a plain function so it can be tested anywhere."""

from __future__ import annotations

import logging
import queue
import sys
import threading
from pathlib import Path

log = logging.getLogger("blackbox")

# the colours of the CRM (neglect/web_static/style.css :root)
BG, WINE, TEXT, MUTED = "#050304", "#1a0609", "#f7efe6", "#b3a59a"
RED, RED2, GOLD, GREEN, AMBER, LINE = "#d0121f", "#ff4452", "#c4bcb4", "#4ade9d", "#f5b942", "#3a1a1d"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
MUTEX_NAME, SHOW_EVENT_NAME = "Local\\NeglectBlackBoxReader", "Local\\NeglectBlackBoxShow"


# ---------------------------------------------------------------- pure helpers (testable anywhere)
def parse_hotkey(text: str) -> tuple[int, int] | None:
    """`ctrl+shift+n` -> (modifier flags, virtual key). One letter or digit plus at least one of ctrl / shift / alt / win."""
    mods, key = 0, ""
    for part in str(text or "").lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control", "strg"):
            mods |= MOD_CONTROL
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part == "alt":
            mods |= MOD_ALT
        elif part in ("win", "windows"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            key = part
        else:
            return None
    if not mods or not key:
        return None
    return mods, ord(key.upper())


def hotkey_label(text: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in str(text or "").lower().replace(" ", "").split("+") if p)


def status_rows(snap: dict) -> list[tuple[str, str, str]]:
    """(label, text, tone) for the window; tone is ok / warn / bad / plain."""
    rows: list[tuple[str, str, str]] = []
    rows.append(("Logging", "paused (you stopped it)" if snap.get("paused") else "on", "warn" if snap.get("paused") else "ok"))
    server, status, linked = snap.get("server"), snap.get("status"), snap.get("linked")
    if server == "down":
        rows.append(("Bot", "not reachable", "bad"))
    elif server == "refused" or status == "revoked":
        rows.append(("Bot", "refused this PC", "bad"))
    elif server == "ok" and not linked:
        rows.append(("Bot", "Discord login not finished", "warn"))
    elif server == "ok" and status != "approved":
        rows.append(("Bot", "waiting for approval", "warn"))
    elif server == "ok":
        rows.append(("Bot", "connected, PC approved", "ok"))
    else:
        rows.append(("Bot", "checking ...", "plain"))
    if snap.get("paused"):
        rows.append(("VRChat", "not being read", "plain"))
    elif snap.get("vrchat") is False:
        rows.append(("VRChat", "not running", "plain"))
    elif snap.get("room"):
        rows.append(("Room", f"{snap['room']}" + (" (recorded)" if snap.get("tracked") else " (not a Neglect instance: nothing is sent)"), "ok" if snap.get("tracked") else "plain"))
    else:
        rows.append(("Room", "waiting for an instance", "plain"))
    if snap.get("ids_allowed"):  # the DEV owner switched the feature on; it stays off until the person does too
        on = bool(snap.get("avatar_ids"))
        rows.append(("Avatar IDs", "on: only the avatar IDs VRChat loaded are read from its analytics file and sent" if on else "off (you can switch it on)", "ok" if on else "plain"))
    rows.append(("Events sent", str(snap.get("sent", 0)), "plain"))
    newest = snap.get("newest")
    rows.append(("Version", f"{snap.get('version', '?')}" + (f"  (newest: {newest})" if newest and newest != snap.get("version") else ""), "plain"))
    return rows


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def assets_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"


# ---------------------------------------------------------------- Windows plumbing (hotkey, only one copy, "show the window" from a second start)
def acquire_single_instance() -> bool:
    """False when another copy of the reader already runs (two readers would send everything twice)."""
    if sys.platform != "win32":
        return True
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    acquire_single_instance.handle = handle  # type: ignore[attr-defined]  # kept alive for the life of the process
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def ask_running_copy_to_show() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    handle = ctypes.windll.kernel32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
    if handle:
        ctypes.windll.kernel32.SetEvent(handle)
        ctypes.windll.kernel32.CloseHandle(handle)


def _listen_for_show(callback) -> None:
    import ctypes

    handle = ctypes.windll.kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    while handle:
        if ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
            callback()


def _hotkey_worker(text: str, on_hotkey, on_fail) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    parsed = parse_hotkey(text)
    if not parsed or not user32.RegisterHotKey(None, 1, parsed[0] | MOD_NOREPEAT, parsed[1]):
        log.info("hotkey %s could not be registered (used by another program?)", text)
        on_fail()
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            on_hotkey()
    user32.UnregisterHotKey(None, 1)


# ---------------------------------------------------------------- the window
class BlackBoxWindow:
    def __init__(self, control, version: str):
        self.control, self.version = control, version
        self.events: queue.Queue[str] = queue.Queue()  # the hotkey and "show" threads only put words in here; the window thread acts on them
        self.hotkey_ok = True
        self.root = None
        self.rows: list = []

    # -- called from other threads
    def request(self, what: str) -> None:
        self.events.put(what)

    # -- main thread
    def run(self, show: bool) -> None:
        import tkinter as tk

        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on a high-resolution screen
        except Exception:
            log.debug("dpi awareness not set", exc_info=True)
        root = self.root = tk.Tk()
        root.title("Neglect Black Box")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.geometry("460x470")
        root.attributes("-toolwindow", True)  # a tool window has no button in the taskbar
        try:
            root.iconbitmap(str(assets_dir() / "logo.ico"))
        except Exception:
            log.debug("window icon not set", exc_info=True)
        root.protocol("WM_DELETE_WINDOW", self.hide)  # the X hides the window; the logger keeps running
        self._build(tk)
        threading.Thread(target=_hotkey_worker, args=(self.control.hotkey, lambda: self.request("toggle"), self._hotkey_failed), daemon=True).start()
        threading.Thread(target=_listen_for_show, args=(lambda: self.request("show"),), daemon=True).start()
        root.withdraw()
        if show:
            self.show()
        root.after(150, self._pump)
        root.after(300, self._refresh)
        root.mainloop()

    def _hotkey_failed(self) -> None:
        self.hotkey_ok = False
        self.request("show")  # without the hotkey the window must at least be there once; "NeglectBlackBox.exe show" (Start menu) opens it again

    def _build(self, tk) -> None:
        root = self.root
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=22, pady=(20, 8))
        try:
            self.logo = tk.PhotoImage(file=str(assets_dir() / "logo.png")).subsample(2, 2)  # 112 px -> 56 px
            tk.Label(head, image=self.logo, bg=BG, borderwidth=0).pack(side="left")
        except Exception:
            self.logo = None
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=14)
        tk.Label(titles, text="N E G L E C T", bg=BG, fg=TEXT, font=("Palatino Linotype", 17, "bold")).pack(anchor="w")
        tk.Label(titles, text="Black Box  ·  VRChat log reader", bg=BG, fg=GOLD, font=("Segoe UI", 10)).pack(anchor="w")
        tk.Frame(root, bg=RED, height=2).pack(fill="x", padx=22, pady=(0, 12))

        card = tk.Frame(root, bg=WINE, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", padx=22)
        for _ in range(6):
            row = tk.Frame(card, bg=WINE)
            row.pack(fill="x", padx=14, pady=5)
            label = tk.Label(row, text="", bg=WINE, fg=MUTED, font=("Segoe UI", 10), width=11, anchor="w")
            label.pack(side="left")
            value = tk.Label(row, text="", bg=WINE, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w", justify="left", wraplength=290)
            value.pack(side="left", fill="x")
            self.rows.append((label, value))

        self.message = tk.Label(root, text="", bg=BG, fg=GOLD, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=410)
        self.message.pack(fill="x", padx=24, pady=(10, 0))

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", padx=22, pady=(12, 0))
        self.stop_button = self._button(tk, buttons, "Stop logging", self._toggle_logging, primary=True)
        self.stop_button.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self._button(tk, buttons, "Check for updates", self.control.ask_for_updates).pack(side="left", expand=True, fill="x", padx=(6, 0))
        self.ids_row = tk.Frame(root, bg=BG)  # only shown while the DEV owner has switched avatar IDs on (see _refresh)
        self.ids_button = self._button(tk, self.ids_row, "Read avatar IDs", self._toggle_ids)
        self.ids_button.pack(fill="x")

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=24, pady=(14, 0))
        self.hint = tk.Label(foot, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=300)
        self.hint.pack(side="left")
        quit_label = tk.Label(foot, text="Exit completely", bg=BG, fg=MUTED, font=("Segoe UI", 9, "underline"), cursor="hand2")
        quit_label.pack(side="right")
        quit_label.bind("<Button-1>", lambda _e: self._quit())

    def _button(self, tk, parent, text, command, primary: bool = False):
        bg, hover = (RED, RED2) if primary else (WINE, "#2b060b")
        button = tk.Button(parent, text=text, command=command, bg=bg, fg=TEXT, activebackground=hover, activeforeground=TEXT, relief="flat", borderwidth=0,
                           highlightbackground=LINE, highlightthickness=0 if primary else 1, font=("Segoe UI", 10, "bold"), cursor="hand2", pady=9)
        button.bind("<Enter>", lambda _e: button.configure(bg=hover))
        button.bind("<Leave>", lambda _e: button.configure(bg=bg))
        return button

    def _toggle_ids(self) -> None:
        self.control.set_avatar_ids(not self.control.avatar_ids)
        self._refresh(again=False)

    def _toggle_logging(self) -> None:
        self.control.set_paused(not self.control.paused)
        self._refresh(again=False)

    def _quit(self) -> None:
        from tkinter import messagebox

        if messagebox.askyesno("Neglect Black Box", "Stop the logger completely?\nIt stays off until you start it again or restart Windows.", parent=self.root):
            self.control.quit.set()
            self.root.destroy()

    def show(self) -> None:
        root = self.root
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(250, lambda: root.attributes("-topmost", False))
        try:
            root.focus_force()
        except Exception:
            log.debug("focus not taken", exc_info=True)

    def hide(self) -> None:
        self.root.withdraw()

    def _pump(self) -> None:
        try:
            while True:
                what = self.events.get_nowait()
                if what == "show":
                    self.show()
                elif what == "toggle":
                    # hide only while the window is really in front: a window that sits behind another program must come forward, not vanish (that took two key presses)
                    self.hide() if self.root.state() == "normal" and self.root.focus_displayof() is not None else self.show()
        except queue.Empty:
            pass
        if not self.control.quit.is_set():
            self.root.after(150, self._pump)

    def _refresh(self, again: bool = True) -> None:
        snap = self.control.snapshot()
        tones = {"ok": GREEN, "warn": AMBER, "bad": RED2, "plain": TEXT}
        data = status_rows(snap)
        for (label, value), (name, text, tone) in zip(self.rows, data, strict=False):
            label.configure(text=name)
            value.configure(text=text, fg=tones.get(tone, TEXT))
        for label, value in self.rows[len(data):]:
            label.configure(text="")
            value.configure(text="")
        self.message.configure(text=snap.get("message", ""))
        self.stop_button.configure(text="Start logging" if snap.get("paused") else "Stop logging")
        if snap.get("ids_allowed"):
            self.ids_button.configure(text="Stop reading avatar IDs" if snap.get("avatar_ids") else "Read avatar IDs")
            self.ids_row.pack(fill="x", padx=22, pady=(8, 0), after=self.stop_button.master)
        else:
            self.ids_row.pack_forget()
        key = hotkey_label(self.control.hotkey)
        self.hint.configure(text=(f"{key} shows or hides this window. The X only hides it: the logger keeps running." if self.hotkey_ok
                                  else f"{key} is used by another program. Open this window with the Neglect Black Box entry in the Start menu."))
        if again and not self.control.quit.is_set():
            self.root.after(1000, self._refresh)


def run_window(control, version: str, show: bool) -> None:
    BlackBoxWindow(control, version).run(show)
