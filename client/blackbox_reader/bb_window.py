"""The Black Box window: a small dark window that lives in the background.

Ctrl+Shift+N (a global hotkey, can be changed in settings.json -> "hotkey") shows or hides it. It has no button in the taskbar and the X only hides it, so
the logger keeps running. It shows what the logger is doing, has "Stop logging" (pauses reading and sending) and "Check for updates", and a small
"Exit completely" for the rare case somebody really wants the program gone until the next start.

Windows only (tkinter + user32 through ctypes); on any other system `available()` is False and the reader just runs without a window.
Everything that is not drawing (hotkey parsing, status text) is a plain function so it can be tested anywhere."""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading
from pathlib import Path
from typing import ClassVar

log = logging.getLogger("blackbox")

# the colours of the CRM (neglect/web_static/style.css :root)
BG, WINE, TEXT, MUTED = "#050304", "#1a0609", "#f7efe6", "#b3a59a"
RED, RED2, GOLD, GREEN, AMBER, LINE = "#d0121f", "#ff4452", "#c4bcb4", "#4ade9d", "#f5b942", "#3a1a1d"
BG_TOP, BG_BOTTOM, GLOW = "#12060a", "#030203", "#9a1424"  # the window background: a dark vertical gradient with a crimson glow behind the logo and a fainter one low right
SURFACE = "#12070a"  # the card behind the status rows (a little darker than WINE, so the hero line stands out)
SANS, SERIF = "Segoe UI", "Palatino Linotype"
WIDTH = 580  # window width in pixels; the height follows the content (see _fit), so nothing is ever cut off
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


def headline(snap: dict) -> tuple[str, str, str]:
    """(title, sentence, tone) for the big line at the top of the window: the one thing the person needs to know. tone is ok / warn / bad / plain."""
    server, status, linked = snap.get("server"), snap.get("status"), snap.get("linked")
    if snap.get("paused"):
        return "Paused", "Nothing is read or sent until you press Start logging.", "warn"
    if server == "down":
        return "Bot not reachable", "The reader keeps trying in the background.", "bad"
    if server == "refused" or status == "revoked":
        return "This PC was refused", "The DEV owner has to check this PC in the dashboard.", "bad"
    if server == "ok" and not linked:
        return "Discord login not finished", "Finish the login in your browser, then this PC can be approved.", "warn"
    if server == "ok" and status != "approved":
        return "Waiting for approval", "The DEV owner approves this PC in the dashboard (Black Box > Devices).", "warn"
    if snap.get("vrchat") is False:
        return "Waiting for VRChat", "Start VRChat: the reader notices it by itself.", "plain"
    if snap.get("room"):
        if snap.get("tracked"):
            return "Recording", f"{snap['room']}", "ok"
        return "Not recording", f"{snap['room']} is not a Neglect instance: nothing is sent.", "plain"
    if server == "ok":
        return "Connected", "Waiting for an instance of the group.", "ok"
    return "Starting", "Checking the connection to the bot ...", "plain"


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
        rows.append(("Avatar IDs", "on: only the avatar IDs are sent" if on else "off (you can switch it on)", "ok" if on else "plain"))
    rows.append(("Events sent", str(snap.get("sent", 0)), "plain"))
    newest = snap.get("newest")
    rows.append(("Version", f"{snap.get('version', '?')}" + (f"  (newest: {newest})" if newest and newest != snap.get("version") else ""), "plain"))
    return rows


def mix(a: str, b: str, t: float) -> str:
    """The colour `t` (0..1) of the way from `a` to `b`, both `#rrggbb`."""
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    t = max(0.0, min(1.0, t))
    return f"#{round(ra + (rb - ra) * t):02x}{round(ga + (gb - ga) * t):02x}{round(ba + (bb - ba) * t):02x}"


def rounded_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    """Corner points for a canvas polygon drawn with smooth=True: a rectangle with round corners (the radius is cut down to what fits)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
            x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def bg_color(x: float, y: float, w: float, h: float) -> str:
    """The colour of the window background at a point: dark top to darker bottom, a crimson glow around the logo (top left) and a fainter one low right.
    Cards take their fill from this too, so they melt into the background instead of sitting on it like boxes."""
    base = mix(BG_TOP, BG_BOTTOM, y / max(h, 1.0))
    d1 = ((x - 0.14 * w) ** 2 + (y - 96) ** 2) / (330.0**2)
    d2 = ((x - 0.96 * w) ** 2 + (y - 0.93 * h) ** 2) / (380.0**2)
    return mix(base, GLOW, min(0.9, 0.5 * math.exp(-2.2 * d1) + 0.26 * math.exp(-2.2 * d2)))


def gradient_rows(w: int, h: int, step: int = 3) -> list[list[str]]:
    """The background as rows of colours, one colour per `step` x `step` pixels (the image is enlarged by `step`, so it stays cheap to build)."""
    return [[bg_color(x * step, y * step, w, h) for x in range(max(1, w // step))] for y in range(max(1, h // step))]


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
    """The window. It draws itself on ONE canvas (round cards, a gradient background that the cards melt into, soft shadows, glossy buttons); if anything in that drawing
    code fails on a PC, it falls back to the plain widget layout - the logger never depends on how the window looks."""

    def __init__(self, control, version: str):
        self.control, self.version = control, version
        self.events: queue.Queue[str] = queue.Queue()  # the hotkey and "show" threads only put words in here; the window thread acts on them
        self.hotkey_ok = True
        self.root = None
        self.tk = None
        self.rows: list = []
        self.fancy = False
        self.canvas = None
        self.logo = None
        self._x: int | None = None  # where the frameless window sits (None = centre it the first time); dragging changes it
        self._y: int | None = None
        self._drag: tuple[int, int] | None = None

    # -- called from other threads
    def request(self, what: str) -> None:
        self.events.put(what)

    # -- main thread
    def run(self, show: bool, threads: bool = True) -> None:
        import tkinter as tk

        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on a high-resolution screen
        except Exception:
            log.debug("dpi awareness not set", exc_info=True)
        self.tk = tk
        root = self.root = tk.Tk()
        root.title("Neglect Black Box")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.geometry(f"{WIDTH}x700")  # a first guess; _fit() sets the real height from the content
        try:
            root.attributes("-toolwindow", True)  # a tool window has no button in the taskbar
        except Exception:
            log.debug("tool window not set", exc_info=True)
        try:
            root.iconbitmap(str(assets_dir() / "logo.ico"))
        except Exception:
            log.debug("window icon not set", exc_info=True)
        root.protocol("WM_DELETE_WINDOW", self.hide)  # the X hides the window; the logger keeps running
        self._build(tk)
        if threads:
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

    # ------------------------------------------------ building: the canvas first, the plain widgets as the safety net
    def _build(self, tk) -> None:
        try:
            self._build_canvas(tk)
            self.fancy = True
            self._set_frameless(True)
        except Exception:
            log.warning("the drawn window failed, using the plain one", exc_info=True)
            self.fancy = False
            self._set_frameless(False)
            try:
                for child in self.root.winfo_children():
                    child.destroy()
            except Exception:
                log.debug("could not clear the window", exc_info=True)
            self.canvas = None
            self._build_plain(tk)

    def _set_frameless(self, on: bool) -> None:
        """The drawn window has no Windows title bar or frame (it brings its own round X and can be dragged anywhere); the plain fallback keeps the normal frame, so it can always be closed."""
        try:
            self.root.overrideredirect(bool(on))
        except Exception:
            log.debug("frame not changed", exc_info=True)

    def _build_canvas(self, tk) -> None:
        self.root.minsize(WIDTH, 0)
        canvas = self.canvas = tk.Canvas(self.root, width=WIDTH, height=700, bg=BG, highlightthickness=0, borderwidth=0, takefocus=1)
        canvas.pack(fill="both", expand=True)
        try:
            self.logo = tk.PhotoImage(file=str(assets_dir() / "logo.png")).zoom(3, 3).subsample(4, 4)  # 112 px -> 84 px
        except Exception:
            self.logo = None
        self._buttons: list[dict] = []
        self._hover = self._pressed = None
        self._focus = None  # index of the button that has the keyboard focus (None = nobody)
        self._has_focus = False
        self._last_key = None
        self._bg_h = 0
        self._bg_img = None
        self._canvas_h = 700
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<Escape>", lambda _e: self.hide() or "break")
        canvas.bind("<FocusIn>", lambda _e: self._set_focus(True))
        canvas.bind("<FocusOut>", lambda _e: self._set_focus(False))
        canvas.bind("<Tab>", lambda _e: self._move_focus(1) or "break")
        canvas.bind("<Shift-Tab>", lambda _e: self._move_focus(-1) or "break")
        canvas.bind("<ISO_Left_Tab>", lambda _e: self._move_focus(-1) or "break")
        for key in ("<Return>", "<space>"):
            canvas.bind(key, lambda _e: self._activate_focus() or "break")
        self._paint(self.control.snapshot())

    # ------------------------------------------------ the drawing
    def _text_height(self, text: str, font, width: int) -> int:
        item = self.canvas.create_text(0, 0, text=text, font=font, width=width, anchor="nw")
        box = self.canvas.bbox(item)
        self.canvas.delete(item)
        return int(box[3] - box[1]) if box else 20

    def _text_width(self, text: str, font) -> int:
        item = self.canvas.create_text(0, 0, text=text, font=font, anchor="nw")
        box = self.canvas.bbox(item)
        self.canvas.delete(item)
        return int(box[2] - box[0]) if box else 80

    def _card(self, x1, y1, x2, y2, fill, radius=18, border=LINE, shadow=True) -> None:
        """A round card with a soft shadow below it and a light edge along its top: it looks raised from the background."""
        c = self.canvas
        if shadow:
            for grow, offset, amount in ((7, 8, 0.55), (3, 4, 0.75)):
                c.create_polygon(rounded_points(x1 - grow + 4, y1 + offset, x2 + grow - 4, y2 + offset, radius + grow), smooth=True, fill=mix(fill, "#000000", amount), outline="", tags="ui")
        c.create_polygon(rounded_points(x1, y1, x2, y2, radius), smooth=True, fill=fill, outline=border, width=1, tags="ui")
        c.create_line(x1 + radius, y1 + 1, x2 - radius, y1 + 1, fill=mix(fill, "#ffffff", 0.16), tags="ui")  # the lit top edge

    def _pill(self, x, y, w, h, text) -> None:
        c = self.canvas
        c.create_polygon(rounded_points(x, y, x + w, y + h, h / 2), smooth=True, fill=mix(WINE, "#000000", 0.25), outline=LINE, tags="ui")
        c.create_text(x + w / 2, y + h / 2, text=text, fill=GOLD, font=(SANS, 10, "bold"), tags="ui")

    def _bead(self, cx, cy, colour, base) -> None:
        """The status light: a few fading rings (the glow) and a small lit ball."""
        c = self.canvas
        for radius, amount in ((22, 0.10), (17, 0.20), (13, 0.36)):
            c.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, fill=mix(base, colour, amount), outline="", tags="ui")
        c.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill=colour, outline=mix(colour, "#000000", 0.4), tags="ui")
        c.create_oval(cx - 5, cy - 6, cx, cy - 2, fill=mix(colour, "#ffffff", 0.55), outline="", tags="ui")  # the highlight

    def _round_button(self, x1, y1, x2, y2, text, command, primary: bool, ring_on: bool) -> None:
        """A glossy round button: a shadow, a body, a lighter cap over the upper half, the text. Hover lightens it, a press sinks it by two pixels."""
        c, n = self.canvas, len(self._buttons)
        body = RED if primary else mix(WINE, "#ffffff", 0.05)
        hover = RED2 if primary else mix(WINE, "#ffffff", 0.14)
        r = 14
        c.create_polygon(rounded_points(x1 + 2, y1 + 5, x2 - 2, y2 + 5, r), smooth=True, fill=mix(BG_TOP, "#000000", 0.7), outline="", tags=("ui", f"sh{n}"))
        shape = c.create_polygon(rounded_points(x1, y1, x2, y2, r), smooth=True, fill=body, outline=mix(body, "#000000", 0.35) if primary else LINE, tags=("ui", f"btn{n}"))
        c.create_polygon(rounded_points(x1 + 3, y1 + 3, x2 - 3, y1 + (y2 - y1) * 0.52, r - 3), smooth=True, fill=mix(body, "#ffffff", 0.2 if primary else 0.07), outline="", tags=("ui", f"btn{n}"))
        c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=text, fill=TEXT, font=(SANS, 12 if primary else 11, "bold"), tags=("ui", f"btn{n}"))
        if ring_on:
            c.create_polygon(rounded_points(x1 - 4, y1 - 4, x2 + 4, y2 + 4, r + 4), smooth=True, fill="", outline=GOLD, width=2, tags="ui")
        self._buttons.append({"cmd": command, "box": (x1, y1, x2, y2), "shape": shape, "colours": (body, hover)})
        c.tag_bind(f"btn{n}", "<Enter>", lambda _e, i=n: self._on_hover(i, True))
        c.tag_bind(f"btn{n}", "<Leave>", lambda _e, i=n: self._on_hover(i, False))

    def _close_button(self, x1, y1, x2, y2, ring_on: bool) -> None:
        """The round X in the top right corner (the window has no Windows frame): a dark disc with a thin cross that turns crimson under the mouse. It only hides the window."""
        c, n = self.canvas, len(self._buttons)
        body, hover = mix(BG_TOP, "#ffffff", 0.07), RED
        shape = c.create_oval(x1, y1, x2, y2, fill=body, outline=LINE, tags=("ui", f"btn{n}"))
        cx, cy, d = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) * 0.2
        for a, b in (((-d, -d), (d, d)), ((-d, d), (d, -d))):
            c.create_line(cx + a[0], cy + a[1], cx + b[0], cy + b[1], fill=TEXT, width=2, capstyle="round", tags=("ui", f"btn{n}"))
        if ring_on:
            c.create_oval(x1 - 3, y1 - 3, x2 + 3, y2 + 3, fill="", outline=GOLD, width=2, tags="ui")
        self._buttons.append({"cmd": self.hide, "box": (x1, y1, x2, y2), "shape": shape, "colours": (body, hover)})
        c.tag_bind(f"btn{n}", "<Enter>", lambda _e, i=n: self._on_hover(i, True))
        c.tag_bind(f"btn{n}", "<Leave>", lambda _e, i=n: self._on_hover(i, False))

    def _on_drag(self, event) -> None:
        """Move the frameless window while the mouse button is held on an empty spot of it."""
        if self._drag is None:
            return
        try:
            self._x = int(getattr(event, "x_root", 0) - self._drag[0])
            self._y = int(getattr(event, "y_root", 0) - self._drag[1])
            self.root.geometry(f"+{self._x}+{self._y}")
        except Exception:
            log.debug("window not moved", exc_info=True)

    def _on_hover(self, i: int, inside: bool) -> None:
        self._hover = i if inside else None
        info = self._buttons[i]
        self.canvas.itemconfigure(info["shape"], fill=info["colours"][1] if inside else info["colours"][0])
        self.canvas.configure(cursor="hand2" if inside else "")

    def _button_at(self, x, y):
        for i, info in enumerate(self._buttons):
            x1, y1, x2, y2 = info["box"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return i
        return None

    def _on_press(self, event) -> None:
        i = self._button_at(getattr(event, "x", -1), getattr(event, "y", -1))
        if i is None:  # an empty spot: this is where the window is picked up and dragged
            try:
                self._drag = (int(getattr(event, "x_root", 0) - self.root.winfo_x()), int(getattr(event, "y_root", 0) - self.root.winfo_y()))
            except Exception:
                self._drag = None
            return
        self._pressed = i
        self.canvas.move(f"btn{i}", 0, 2)
        self.canvas.itemconfigure(f"sh{i}", state="hidden")  # the button sinks into the page: no shadow while it is down

    def _on_release(self, event) -> None:
        """One handler for the whole canvas, so a release anywhere (also outside the window) always puts the button back; it is a click only when it ends on the same button."""
        self._drag = None
        i, self._pressed = self._pressed, None
        if i is None:
            return
        self.canvas.move(f"btn{i}", 0, -2)
        self.canvas.itemconfigure(f"sh{i}", state="normal")
        if self._button_at(getattr(event, "x", -1), getattr(event, "y", -1)) == i:
            self._buttons[i]["cmd"]()

    def _set_focus(self, has: bool) -> None:
        self._has_focus = has
        if has and self._focus is None and getattr(self, "_buttons", None):
            self._focus = 0
        self._last_key = None  # repaint with or without the focus ring
        self._refresh(again=False)

    def _move_focus(self, step: int) -> None:
        if getattr(self, "_buttons", None):
            self._focus = ((self._focus if self._focus is not None else -step) + step) % len(self._buttons)
            self._last_key = None
            self._refresh(again=False)

    def _activate_focus(self) -> None:
        if self._focus is not None and self._focus < len(self._buttons):
            self._buttons[self._focus]["cmd"]()

    def _paint(self, snap: dict) -> None:
        """Draw everything from the snapshot. Called again only when the snapshot (or the keyboard focus) changed, so a click is never swallowed by a repaint."""
        c = self.canvas
        c.delete("ui")
        self._buttons = []
        W, pad = WIDTH, 30
        H = self._canvas_h  # what the background was made for; if the content ends up taller or shorter, the second pass below redraws with the right size
        tones = {"ok": GREEN, "warn": AMBER, "bad": RED2, "plain": MUTED}
        y = 28
        c.create_rectangle(0, 0, W - 1, H - 1, outline=mix(LINE, RED, 0.3), fill="", tags="ui")  # a hairline: without the Windows frame it keeps the edge clean against the desktop
        if self.logo is not None:
            c.create_image(pad, y, image=self.logo, anchor="nw", tags="ui")
        tx = pad + (102 if self.logo is not None else 0)
        c.create_text(tx, y + 8, text="N E G L E C T", anchor="nw", fill=TEXT, font=(SERIF, 25, "bold"), tags="ui")
        c.create_text(tx, y + 50, text="Black Box  ·  VRChat log reader", anchor="nw", fill=GOLD, font=(SANS, 11), tags="ui")
        self._pill(W - pad - 66 - 18, y + 2, 66, 26, f"v{self.version}")  # left of the round X
        y += 84 + 20
        c.create_line(pad, y, W - pad, y, fill=mix(RED, "#000000", 0.25), width=3, tags="ui")
        c.create_line(pad, y, W - pad, y, fill=RED2, width=1, tags="ui")
        y += 24

        # -- the one thing to know
        title, sentence, tone = headline(snap)
        colour = tones.get(tone, MUTED)
        wrap = W - 2 * pad - 130
        sh = self._text_height(sentence, (SANS, 11), wrap)
        card_h = max(88, 30 + 28 + 4 + sh)
        fill = mix(bg_color(W / 2, y + card_h / 2, W, H), WINE, 0.62)
        self._card(pad, y, W - pad, y + card_h, fill)
        self._bead(pad + 46, y + card_h / 2, colour, fill)
        c.create_text(pad + 86, y + 22, text=title, anchor="nw", fill=TEXT, font=(SANS, 19, "bold"), tags="ui")
        c.create_text(pad + 86, y + 56, text=sentence, anchor="nw", fill=MUTED, font=(SANS, 11), width=wrap, tags="ui")
        y += card_h + 18

        # -- the details
        data = status_rows(snap)
        label_w = max((self._text_width(name, (SANS, 11)) for name, _text, _tone in data), default=80)  # measured, so a larger Windows text scale cannot make label and value run into each other
        vx = pad + 44 + label_w + 22
        vwrap = W - pad - 22 - vx
        heights = [max(34, self._text_height(text, (SANS, 11, "bold"), vwrap) + 14) for _name, text, _tone in data]
        total = sum(heights) + 12
        fill = mix(bg_color(W / 2, y + total / 2, W, H), SURFACE, 0.8)
        self._card(pad, y, W - pad, y + total, fill, radius=16)
        ry = y + 6
        for (name, text, row_tone), height in zip(data, heights, strict=False):
            mid = ry + height / 2
            dot = tones.get(row_tone, MUTED)
            c.create_oval(pad + 20, mid - 4, pad + 28, mid + 4, fill=dot, outline=mix(dot, "#000000", 0.45), tags="ui")
            c.create_text(pad + 44, mid, text=name, anchor="w", fill=MUTED, font=(SANS, 11), tags="ui")
            c.create_text(vx, mid, text=text, anchor="w", fill=TEXT if row_tone == "plain" else dot, font=(SANS, 11, "bold"), width=vwrap, tags="ui")
            ry += height
            if height is not heights[-1]:
                c.create_line(pad + 20, ry, W - pad - 20, ry, fill=mix(fill, "#ffffff", 0.06), tags="ui")
        y += total + 14
        if snap.get("message"):
            mh = self._text_height(str(snap["message"]), (SANS, 10), W - 2 * pad)
            c.create_text(pad, y, text=str(snap["message"]), anchor="nw", fill=GOLD, font=(SANS, 10), width=W - 2 * pad, tags="ui")
            y += mh + 12
        else:
            y += 4

        # -- the buttons
        focus = self._focus if self._has_focus else None
        self._round_button(pad, y, W - pad, y + 50, "Start logging" if snap.get("paused") else "Stop logging", self._toggle_logging, True, focus == 0)
        y += 50 + 14
        if snap.get("ids_allowed"):
            gap = 12
            half = (W - 2 * pad - gap) / 2
            self._round_button(pad, y, pad + half, y + 44, "Check for updates", self.control.ask_for_updates, False, focus == 1)
            self._round_button(pad + half + gap, y, W - pad, y + 44, "Stop reading avatar IDs" if snap.get("avatar_ids") else "Read avatar IDs", self._toggle_ids, False, focus == 2)
        else:
            self._round_button(pad, y, W - pad, y + 44, "Check for updates", self.control.ask_for_updates, False, focus == 1)
        y += 44 + 22
        self._close_button(W - 16 - 24, 14, W - 16, 38, focus == len(self._buttons))  # drawn last: it is the last stop of the Tab key

        # -- the footer
        key = hotkey_label(self.control.hotkey)
        hint = (f"{key} shows or hides this window. The X only hides it: the logger keeps running. Drag the window by an empty spot." if self.hotkey_ok
                else f"{key} is used by another program. Open this window with the Neglect Black Box entry in the Start menu.")
        hh = self._text_height(hint, (SANS, 9), W - 2 * pad - 130)
        c.create_text(pad, y, text=hint, anchor="nw", fill=MUTED, font=(SANS, 9), width=W - 2 * pad - 130, tags="ui")
        quit_item = c.create_text(W - pad, y, text="Exit completely", anchor="ne", fill=MUTED, font=(SANS, 9, "underline"), tags=("ui", "quit"))
        c.tag_bind("quit", "<Button-1>", lambda _e: self._quit())
        c.tag_bind("quit", "<Enter>", lambda _e: (c.configure(cursor="hand2"), c.itemconfigure(quit_item, fill=TEXT)))
        c.tag_bind("quit", "<Leave>", lambda _e: (c.configure(cursor=""), c.itemconfigure(quit_item, fill=MUTED)))
        y += max(hh, 16) + 28

        new_h = int(y)
        if new_h != H:  # the background and the blended card fills depend on the height: once more with the right one
            self._canvas_h = new_h
            c.configure(height=new_h)
            return self._paint(snap)
        self._ensure_background(new_h)

    def _ensure_background(self, h: int) -> None:
        """The gradient image behind everything (built once per height; half-way coarse and enlarged, so it costs a fraction of a second)."""
        c = self.canvas
        if self._bg_h != h or self._bg_img is None:
            try:
                step = 3
                rows = gradient_rows(WIDTH, h, step)
                img = self.tk.PhotoImage(width=len(rows[0]), height=len(rows))
                img.put(" ".join("{" + " ".join(row) + "}" for row in rows))
                self._bg_img = img.zoom(step, step)
                self._bg_h = h
            except Exception:
                log.debug("background gradient not drawn", exc_info=True)
                self._bg_img = None
                self._bg_h = h
        c.delete("bg")
        if self._bg_img is not None:
            c.create_image(0, 0, image=self._bg_img, anchor="nw", tags="bg")
            c.tag_lower("bg")

    # ------------------------------------------------ the plain layout (the safety net, and what an old Tk without canvas features would get)
    def _build_plain(self, tk) -> None:
        root = self.root
        pad = 30
        root.minsize(WIDTH, 0)
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=pad, pady=(26, 0))
        try:
            self.logo = tk.PhotoImage(file=str(assets_dir() / "logo.png")).zoom(3, 3).subsample(4, 4)  # 112 px -> 84 px
            tk.Label(head, image=self.logo, bg=BG, borderwidth=0).pack(side="left")
        except Exception:
            self.logo = None
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=18)
        tk.Label(titles, text="N E G L E C T", bg=BG, fg=TEXT, font=(SERIF, 24, "bold")).pack(anchor="w")
        tk.Label(titles, text="Black Box  ·  VRChat log reader", bg=BG, fg=GOLD, font=(SANS, 11)).pack(anchor="w", pady=(2, 0))
        tk.Label(head, text=f"v{self.version}", bg=WINE, fg=GOLD, font=(SANS, 10, "bold"), padx=11, pady=4, highlightbackground=LINE, highlightthickness=1).pack(side="right", anchor="n")
        tk.Frame(root, bg=RED, height=2).pack(fill="x", padx=pad, pady=(18, 20))

        hero = tk.Frame(root, bg=WINE, highlightbackground=LINE, highlightthickness=1)
        hero.pack(fill="x", padx=pad)
        inner = tk.Frame(hero, bg=WINE)
        inner.pack(fill="x", padx=22, pady=18)
        self.hero_dot = tk.Label(inner, text="●", bg=WINE, fg=GREEN, font=(SANS, 26))
        self.hero_dot.pack(side="left", padx=(0, 16))
        words = tk.Frame(inner, bg=WINE)
        words.pack(side="left", fill="x", expand=True)
        self.hero_title = tk.Label(words, text="", bg=WINE, fg=TEXT, font=(SANS, 18, "bold"), anchor="w", justify="left")
        self.hero_title.pack(fill="x")
        self.hero_text = tk.Label(words, text="", bg=WINE, fg=MUTED, font=(SANS, 11), anchor="w", justify="left", wraplength=WIDTH - 2 * pad - 110)
        self.hero_text.pack(fill="x", pady=(3, 0))

        card = tk.Frame(root, bg=SURFACE, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", padx=pad, pady=(14, 0))
        self.rows = []
        for _ in range(7):  # more slots than rows: the unused ones are hidden
            row = tk.Frame(card, bg=SURFACE)
            dot = tk.Label(row, text="●", bg=SURFACE, fg=MUTED, font=(SANS, 9))
            dot.pack(side="left", padx=(16, 10))
            label = tk.Label(row, text="", bg=SURFACE, fg=MUTED, font=(SANS, 11), width=11, anchor="w")
            label.pack(side="left")
            value = tk.Label(row, text="", bg=SURFACE, fg=TEXT, font=(SANS, 11, "bold"), anchor="w", justify="left", wraplength=WIDTH - 2 * pad - 190)
            value.pack(side="left", fill="x", expand=True, padx=(0, 16))
            self.rows.append((row, dot, label, value))
        tk.Frame(card, bg=SURFACE, height=6).pack()  # a little air under the last row

        self.message = tk.Label(root, text="", bg=BG, fg=GOLD, font=(SANS, 10), anchor="w", justify="left", wraplength=WIDTH - 2 * pad)
        self.message.pack(fill="x", padx=pad, pady=(12, 0))

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", padx=pad, pady=(14, 0))
        self.stop_button = self._button(tk, buttons, "Stop logging", self._toggle_logging, primary=True)
        self.stop_button.pack(fill="x")
        self.second_row = tk.Frame(buttons, bg=BG)
        self.second_row.pack(fill="x", pady=(10, 0))
        self.update_button = self._button(tk, self.second_row, "Check for updates", self.control.ask_for_updates)
        self.update_button.pack(side="left", expand=True, fill="x")
        self.ids_button = self._button(tk, self.second_row, "Read avatar IDs", self._toggle_ids)  # only shown while the DEV owner allowed it (see _refresh_plain)
        self.ids_shown = False

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=pad, pady=(18, 24))
        self.hint = tk.Label(foot, text="", bg=BG, fg=MUTED, font=(SANS, 9), anchor="w", justify="left", wraplength=WIDTH - 2 * pad - 130)
        self.hint.pack(side="left", fill="x", expand=True)
        quit_label = tk.Label(foot, text="Exit completely", bg=BG, fg=MUTED, font=(SANS, 9, "underline"), cursor="hand2", takefocus=1)
        quit_label.pack(side="right", anchor="n")
        quit_label.bind("<Button-1>", lambda _e: self._quit())
        quit_label.bind("<Return>", lambda _e: self._quit())

    def _button(self, tk, parent, text, command, primary: bool = False):
        """(plain layout) A flat button: big, a clear hover and a visible gold ring while it has the keyboard focus."""
        bg, hover = (RED, RED2) if primary else (WINE, "#2b060b")
        button = tk.Button(parent, text=text, command=command, bg=bg, fg=TEXT, activebackground=hover, activeforeground=TEXT, relief="flat", borderwidth=0,
                           highlightbackground=bg if primary else LINE, highlightcolor=GOLD, highlightthickness=2 if primary else 1, font=(SANS, 12 if primary else 11, "bold"),
                           cursor="hand2", pady=13 if primary else 11, takefocus=1)
        button.bind("<Enter>", lambda _e: button.configure(bg=hover))
        button.bind("<Leave>", lambda _e: button.configure(bg=bg))
        return button

    # ------------------------------------------------ actions, size, show / hide
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

    def _geometry(self, height: int) -> str:
        """Size, and for the frameless window also the place: the first time it is centred on the screen (a little above the middle), later it stays where it was dragged to."""
        if not self.fancy:
            return f"{WIDTH}x{height}"
        if self._x is None or self._y is None:
            try:
                self._x = max(0, (int(self.root.winfo_screenwidth()) - WIDTH) // 2)
                self._y = max(0, (int(self.root.winfo_screenheight()) - height) // 2 - 40)
            except Exception:
                self._x, self._y = 100, 100
        return f"{WIDTH}x{height}+{self._x}+{self._y}"

    def _fit(self) -> None:
        """Size the window to its content (the width is fixed): a new row or a longer sentence can never push a button or the hint out of sight."""
        try:
            self.root.update_idletasks()
            height = int(self._canvas_h) if self.fancy else int(self.root.winfo_reqheight())
            if height != getattr(self, "_height", 0):
                self._height = height
                self.root.geometry(self._geometry(height))
        except Exception:
            log.debug("window not resized", exc_info=True)

    def show(self) -> None:
        root = self.root
        root.deiconify()
        self._fit()
        root.lift()
        root.attributes("-topmost", True)
        root.after(250, lambda: root.attributes("-topmost", False))
        try:
            (self.canvas or root).focus_force()
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
        if self.fancy:
            key = (repr(sorted(snap.items())), self.hotkey_ok, self._focus, self._has_focus, self.control.hotkey)
            if key != self._last_key and self._pressed is None:  # nothing changed (or a click is in progress): leave the drawing alone
                self._last_key = key
                try:
                    self._paint(snap)
                except Exception:
                    log.warning("drawing the window failed, switching to the plain one", exc_info=True)
                    self.fancy = False
                    try:
                        self.canvas.destroy()
                    except Exception:
                        log.debug("canvas not removed", exc_info=True)
                    self.canvas = None
                    try:  # a frame can only be switched while the window is hidden
                        self.root.withdraw()
                        self._set_frameless(False)
                        self.root.deiconify()
                    except Exception:
                        log.debug("frame not restored", exc_info=True)
                    self._build_plain(self.tk)
                    self._height = 0  # the plain window needs its own size
                    self._refresh_plain(snap)
        else:
            self._refresh_plain(snap)
        self._fit()
        if again and not self.control.quit.is_set():
            self.root.after(1000, self._refresh)

    def _refresh_plain(self, snap: dict) -> None:
        tones = {"ok": GREEN, "warn": AMBER, "bad": RED2, "plain": MUTED}
        title, sentence, tone = headline(snap)
        self.hero_dot.configure(fg=tones.get(tone, MUTED))
        self.hero_title.configure(text=title)
        self.hero_text.configure(text=sentence)
        data = status_rows(snap)
        for (row, dot, label, value), (name, text, row_tone) in zip(self.rows, data, strict=False):
            label.configure(text=name)
            value.configure(text=text, fg=TEXT if row_tone == "plain" else tones.get(row_tone, TEXT))
            dot.configure(fg=tones.get(row_tone, MUTED))
            row.pack(fill="x", pady=(8, 0))
        for row, *_rest in self.rows[len(data):]:
            row.pack_forget()
        self.message.configure(text=snap.get("message", ""))
        self.stop_button.configure(text="Start logging" if snap.get("paused") else "Stop logging")
        allowed = bool(snap.get("ids_allowed"))
        if allowed:
            self.ids_button.configure(text="Stop reading avatar IDs" if snap.get("avatar_ids") else "Read avatar IDs")
        if allowed != self.ids_shown:
            self.ids_shown = allowed
            if allowed:
                self.ids_button.pack(side="left", expand=True, fill="x", padx=(10, 0))
            else:
                self.ids_button.pack_forget()
        key = hotkey_label(self.control.hotkey)
        self.hint.configure(text=(f"{key} shows or hides this window. The X only hides it: the logger keeps running." if self.hotkey_ok
                                  else f"{key} is used by another program. Open this window with the Neglect Black Box entry in the Start menu."))


def run_window(control, version: str, show: bool) -> None:
    BlackBoxWindow(control, version).run(show)


# ---------------------------------------------------------------- a preview: `python bb_window.py` shows the window with made-up data (no reader, no network), the states take turns
class _DemoControl:
    """Stands in for the reader's Control: the same attributes, with a few made-up states that change every few seconds."""

    STATES: ClassVar[list[dict]] = [
        {"server": "ok", "status": "approved", "linked": True, "room": "Just B Club 4.0", "tracked": True, "sent": 305, "ids_allowed": True, "avatar_ids": True, "newest": "0.5.2", "message": "You have the newest version (0.5.2)"},
        {"server": "ok", "status": "approved", "linked": True, "vrchat": False, "sent": 0},
        {"server": "ok", "status": "approved", "linked": True, "room": "My home", "tracked": False, "sent": 12, "ids_allowed": True, "avatar_ids": False},
        {"paused": True, "server": "ok", "status": "approved", "linked": True, "sent": 40},
        {"server": "ok", "status": "pending", "linked": True},
        {"server": "down"},
    ]

    def __init__(self):
        self.quit = threading.Event()
        self.check_now = threading.Event()
        self.paused = False
        self.avatar_ids = True
        self.hotkey = "ctrl+shift+n"
        self.i = 0
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        import time

        state = dict(self.STATES[int(time.time() // 5) % len(self.STATES)])
        state.setdefault("version", "0.5.2")
        state["paused"] = state.get("paused") or self.paused
        state["avatar_ids"] = self.avatar_ids if state.get("ids_allowed") else state.get("avatar_ids", False)
        return state

    def set_paused(self, value: bool) -> None:
        self.paused = bool(value)

    def set_avatar_ids(self, value: bool) -> None:
        self.avatar_ids = bool(value)

    def ask_for_updates(self) -> None:
        pass


if __name__ == "__main__":  # pragma: no cover - needs a screen
    logging.basicConfig(level=logging.INFO)
    BlackBoxWindow(_DemoControl(), "0.5.2").run(show=True, threads=False)
