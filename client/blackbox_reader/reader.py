"""Neglect Black Box reader - runs invisibly on a staff PC and sends VRChat log EVENTS (never the log, never any login) to the bot.

Only a logger: no actions. It recognises the instance from the log, asks the bot whether that instance is one it knows (`hello`), and only then sends
the compact events of that room. Leaving the room, a crash or a restart of VRChat ends the recording. Every request is signed with this PC's own
Ed25519 key (the private key never leaves the PC; on Windows it is wrapped by DPAPI) and the PC has to be approved by the DEV owner in the dashboard first.

Run:  reader.py run       (what autostart runs; hidden when built with --noconsole). The first start creates the key, registers this PC and opens the browser
                          once: the person logs in with Discord (that proves who they are), then the DEV owner approves the PC in the dashboard.
      reader.py show      (like run, and the window is opened; when the reader already runs it only opens that window)
      reader.py selftest  (parser + signing check, writes selftest.txt; the build runs it on the finished exe)
Build + installer: see client/blackbox_reader/README.md (Windows runner, GitHub Actions)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import blackbox_parse as bp
except ImportError:  # running from the repo: the parser lives in neglect/ (pure standard library)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "neglect"))
    import blackbox_parse as bp

try:
    import bb_window as ui
except ImportError:  # started from another folder (tests, tools): the module sits next to this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import bb_window as ui
    except ImportError:
        ui = None  # type: ignore[assignment]  # the window is optional (and only exists on Windows)

VERSION = "0.5.3"
UPDATE_PREFIXES = ("https://github.com/SparkTobiachii/NeglectBotDashboard/releases/download/", "https://github.com/SparkTobiachii/NeglectBot/releases/download/", "https://github.com/SparkTobiachii/NeglectBlackBox/releases/download/")
UPDATE_PREFIX = UPDATE_PREFIXES[0]  # an update is only ever fetched from our own GitHub releases ...
UPDATE_FROM_BOT = "bot"  # ... or from the bot itself (the repository is private: the bot serves the installer, signed like every other request)
INSTALLER_MAX = 200 * 1024 * 1024
CHECK_EVERY = 300.0
DEFAULT_SERVER = "https://crm.neglect404.org"
BATCH = 200
QUEUE_MAX = 5000  # events kept while the server cannot be reached
HEARTBEAT = 30.0
IDS_WINDOW = 120.0  # seconds after a room change in which the list of avatar IDs VRChat writes belongs to the room that was left
IDS_MAX_FILE = 16 * 1024 * 1024  # a bigger analytics file is not read at all (the real one is small)
IDS_RETRY_MAX = 400
log = logging.getLogger("blackbox")


# ---------------------------------------------------------------- where things live
def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "NeglectBlackBox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def vrchat_log_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:  # %LOCALAPPDATA%\..\LocalLow\VRChat\VRChat
        return Path(local).parent / "LocalLow" / "VRChat" / "VRChat"
    return Path.home() / ".config" / "VRChat"  # only for development on another OS


def newest_log(folder: Path) -> Path | None:
    files = sorted(folder.glob("output_log_*.txt"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def amplitude_cache_path() -> Path:
    """VRChat's analytics buffer `%TEMP%\\VRChat\\VRChat\\amplitude.cache`. Besides personal data of the person it holds the IDs of the avatars the game loaded."""
    return Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()) / "VRChat" / "VRChat" / "amplitude.cache"


class AmplitudeTail:
    """Looks at the analytics file for avatar IDs - and ONLY for those: `poll` returns the avatar IDs that were not in the file before, nothing else of the file is kept,
    logged or sent. The first look after `reset` only learns what is already there (history is never sent). A file that did not change is not read (stat only)."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: set[str] | None = None  # None = not learned yet
        self.stamp: tuple[int, float] | None = None

    def reset(self) -> None:
        self.seen, self.stamp = None, None

    def poll(self) -> list[str]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self.stamp == (st.st_size, st.st_mtime):
            return []
        self.stamp = (st.st_size, st.st_mtime)
        if st.st_size > IDS_MAX_FILE:
            return []
        try:
            data = self.path.read_bytes()
        except OSError:
            self.stamp = None  # locked by VRChat right now: look again next time
            return []
        ids = bp.avatar_ids_from(data)
        if self.seen is None:
            self.seen = set(ids)
            return []
        fresh = [i for i in ids if i not in self.seen]
        self.seen.update(fresh)
        if len(self.seen) > 5000:  # the file is cleared by VRChat from time to time; this is only a safety net
            self.seen = set(ids)
        return fresh


# ---------------------------------------------------------------- key protection (Windows DPAPI; elsewhere the file is just not world readable)
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData  # type: ignore[attr-defined]
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree  # type: ignore[attr-defined]
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        free(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    return _dpapi(data, True) if sys.platform == "win32" else data


def unprotect(data: bytes) -> bytes:
    return _dpapi(data, False) if sys.platform == "win32" else data


class Identity:
    """This PC: an Ed25519 key pair, the Discord ID of the person, the server address."""

    def __init__(self, folder: Path):
        self.path = folder / "device.json"
        self.cfg: dict = {}
        if self.path.exists():
            self.cfg = json.loads(self.path.read_text("utf-8"))

    @property
    def ready(self) -> bool:
        return bool(self.cfg.get("key") and self.cfg.get("device_id"))

    def link_url(self) -> str:
        return f"{self.cfg['server']}/bb/link?d={self.cfg['device_id']}"

    def create(self, server: str) -> None:
        from nacl.signing import SigningKey

        key = SigningKey.generate()
        pub = base64.b64encode(bytes(key.verify_key)).decode()
        self.cfg = {"server": server.rstrip("/"), "key": base64.b64encode(protect(bytes(key))).decode(), "pub": pub,
                    "device_id": hashlib.sha256(pub.encode()).hexdigest()[:20]}
        self.path.write_text(json.dumps(self.cfg), "utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def signing_key(self):
        from nacl.signing import SigningKey

        return SigningKey(unprotect(base64.b64decode(self.cfg["key"])))


# ---------------------------------------------------------------- talking to the bot
class Api:
    ids_state: bool | None = None

    def __init__(self, identity: Identity):
        self.id = identity
        self.key = None

    def _call(self, path: str, body: dict, signed: bool = True, timeout: float = 20.0) -> dict:
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"NeglectBlackBox/{VERSION}"}
        if signed:
            if self.key is None:
                self.key = self.id.signing_key()
            ts, nonce = str(time.time()), secrets.token_hex(12)
            msg = f"POST\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(raw).hexdigest()}".encode()
            headers.update({"X-BB-Device": self.id.cfg["device_id"], "X-BB-Ts": ts, "X-BB-Nonce": nonce, "X-BB-Sig": base64.b64encode(self.key.sign(msg).signature).decode()})
        if not self.id.cfg["server"].startswith("https://"):
            raise ApiError(0, "the server address must be https")
        req = urllib.request.Request(self.id.cfg["server"] + path, data=raw, headers=headers, method="POST")  # noqa: S310  # nosec B310 - https only (checked above)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read() or b"{}").get("error", "")
            except (ValueError, OSError):
                detail = ""
            raise ApiError(e.code, detail) from e

    def download(self, path: str, target: Path) -> bool:
        """A signed GET of a file (the installer the bot offers). The file is only kept when it arrived completely and is not absurdly big."""
        if self.key is None:
            self.key = self.id.signing_key()
        if not self.id.cfg["server"].startswith("https://"):
            raise ApiError(0, "the server address must be https")
        ts, nonce = str(time.time()), secrets.token_hex(12)
        msg = f"GET\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(b'').hexdigest()}".encode()
        headers = {"User-Agent": f"NeglectBlackBox/{VERSION}", "X-BB-Device": self.id.cfg["device_id"], "X-BB-Ts": ts, "X-BB-Nonce": nonce,
                   "X-BB-Sig": base64.b64encode(self.key.sign(msg).signature).decode()}
        req = urllib.request.Request(self.id.cfg["server"] + path, headers=headers, method="GET")  # noqa: S310  # nosec B310 - https only (checked above)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as out:  # noqa: S310  # nosec B310
                size = 0
                while chunk := resp.read(1 << 20):
                    size += len(chunk)
                    if size > INSTALLER_MAX:
                        target.unlink(missing_ok=True)
                        return False
                    out.write(chunk)
        except urllib.error.HTTPError as e:
            target.unlink(missing_ok=True)
            raise ApiError(e.code, "download refused") from e
        return True

    def register(self, name: str) -> dict:
        return self._call("/bb/v1/register", {"name": name, "pubkey": self.id.cfg["pub"], "hw": hardware_hash(), "version": VERSION}, signed=False)

    def hello(self, loc: str, debug: bool | None = None) -> dict:
        body: dict = {"loc": loc, "version": VERSION}
        if debug is not None:
            body["debug"] = debug
        if self.ids_state is not None:
            body["avatar_ids"] = self.ids_state  # does this PC read avatar IDs right now (for the Devices tab)
        return self._call("/bb/v1/hello", body)

    def send(self, events: list[dict]) -> dict:
        return self._call("/bb/v1/events", {"events": events})


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status} {detail}")
        self.status, self.detail = status, detail


def hardware_hash() -> str:
    """Info for the approver only (it can be faked, the key is the real binding)."""
    return hashlib.sha256(f"{platform.node()}|{platform.machine()}|{os.environ.get('PROCESSOR_IDENTIFIER', '')}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------- the state machine (no I/O of its own: testable anywhere)
class Reader:
    """Feeds log lines to the parser and decides what leaves the PC: nothing until the bot says the instance is tracked."""

    def __init__(self, api, device_id: str, tz=None):
        self.api, self.device = api, device_id
        self.parser = bp.LogParser(tz)
        self.loc = ""
        self.track: bool | None = False  # None = the bot could not be asked yet
        self.queue: list[dict] = []
        self.replaying = False
        self.last_hello = 0.0
        self.count = 0
        self.sent = 0  # events the bot has accepted since this program started
        self.log_name = ""
        self.ids_allowed = False  # the DEV owner's switch, as the bot answered it
        self.ids_on = False  # both switches (the person's and the DEV owner's) are on right now: set by the loop
        self.room_ids: set[str] = set()  # IDs from the log already handed on in this room
        self.prev_loc, self.prev_track = "", False  # the room that was left last (and whether it was tracked)
        self.entered_at = self.left_at = 0.0
        self.ids_sent = 0
        self.id_retry: list[dict] = []
        self.last_id_try = 0.0

    def start_replay(self, log_name: str) -> None:
        self.log_name, self.replaying = log_name, True

    def end_replay(self) -> None:
        self.replaying = False
        self.flush()

    def feed(self, line: str) -> None:
        for ev in self.parser.feed(line):
            self.handle(ev)

    def handle(self, ev: dict) -> None:
        self.count += 1
        ev["eid"] = hashlib.sha256(f"{self.device}|{self.log_name}|{self.count}".encode()).hexdigest()[:20]
        kind = ev["kind"]
        if kind == "avatar_id":  # from the debug log: only while avatar IDs are switched on (both sides), once per room
            if not self.ids_on or self.replaying or ev["data"].get("id") in self.room_ids:
                return
            self.room_ids.add(ev["data"].get("id"))
        if kind == "room_enter":
            self.room_ids.clear()
            self.queue.clear()  # an older room that was never flushed is not interesting
            if self.loc:  # no OnLeftRoom line came: the room is left now
                self.prev_loc, self.prev_track, self.left_at = self.loc, self.track is True, time.time()
            self.loc = ev["loc"]
            self.entered_at = time.time()
            self.track = None
            self.queue.append(ev)
            if not self.replaying:
                self.ask()
            return
        if not self.loc or self.track is False:
            return  # not in a room / not an instance the bot knows: nothing is kept, nothing is sent
        self.queue.append(ev)
        if kind == "room_leave":
            if not self.replaying:
                self.flush()
            self.prev_loc, self.prev_track, self.left_at = self.loc, self.track is True, time.time()
            self.loc, self.track = "", False
            if self.replaying:
                self.queue.clear()

    def ask(self) -> None:
        try:
            answer = self.api.hello(self.loc)
            self.track = bool(answer.get("track"))
            self.ids_allowed = bool(answer.get("avatar_ids"))
            self.last_hello = time.time()
        except Exception as e:
            self.track = None
            log.info("hello failed: %s", e)
        if self.track is False:
            self.queue.clear()

    def flush(self) -> bool:
        if self.track is None and self.loc and not self.replaying:
            self.ask()
        if not self.track or not self.queue or self.replaying:
            return True
        while self.queue:
            batch = self.queue[:BATCH]
            try:
                self.api.send(batch)
            except Exception as e:
                log.info("send failed: %s", e)
                del self.queue[:-QUEUE_MAX]
                return False
            del self.queue[: len(batch)]
            self.sent += len(batch)
        return True

    def heartbeat(self, now: float) -> None:
        if self.loc and now - self.last_hello >= HEARTBEAT:
            self.ask()
            if self.track:
                self.flush()
        self.retry_ids(now)

    def stop_recording(self) -> None:
        """VRChat closed or crashed: say so and forget the room."""
        if self.loc and self.track:
            self.queue.append({"ts": time.time(), "kind": "room_leave", "loc": self.loc, "data": {"reason": "closed"}, "eid": secrets.token_hex(8)})
            self.flush()
        if self.loc:
            self.prev_loc, self.prev_track, self.left_at = self.loc, self.track is True, time.time()
        self.loc, self.track = "", False
        self.queue.clear()
        self.parser.reset_room()

    # ------------------------------------------------ avatar IDs (opt-in on both sides, see `AmplitudeTail`)
    def id_room(self, now: float) -> str:
        """The room new avatar IDs belong to. VRChat writes the list of the avatars it loaded when the room CHANGES, a little after the new room was entered, so for IDs
        that arrive within IDS_WINDOW of a room change it is the room that was left (only when that one was tracked); later it is the room the person is in
        (an avatar switched on by hand is written at once). Anything else (an untracked room, no room) belongs nowhere: nothing is sent."""
        if self.loc and now - self.entered_at >= IDS_WINDOW:
            return self.loc if self.track is True else ""
        if self.prev_track and now - (self.entered_at if self.loc else self.left_at) <= IDS_WINDOW:
            return self.prev_loc
        return ""

    def feed_ids(self, ids: list[str], now: float | None = None) -> int:
        """Send avatar IDs, each as its own `avatar_id` event of the room they belong to. Returns how many were handed to the bot. Not while a log is being replayed."""
        now = time.time() if now is None else now
        loc = self.id_room(now)
        if not ids or not loc or self.replaying:
            return 0
        events = [{"ts": now, "kind": "avatar_id", "loc": loc, "data": {"id": i}, "eid": hashlib.sha256(f"{self.device}|avtr|{loc}|{i}".encode()).hexdigest()[:20]} for i in ids if bp.AVTR_RE.match(i)]
        return self._send_ids(events + self.id_retry, now)

    def _send_ids(self, events: list[dict], now: float) -> int:
        self.id_retry = []
        done = 0
        for n in range(0, len(events), BATCH):
            batch = events[n:n + BATCH]
            try:
                self.api.send(batch)
            except Exception as e:
                log.info("avatar IDs not sent: %s", e)
                self.id_retry = events[n:][-IDS_RETRY_MAX:]
                self.last_id_try = now
                break
            done += len(batch)
        self.ids_sent += done
        self.sent += done
        return done

    def retry_ids(self, now: float) -> None:
        if self.id_retry and now - self.last_id_try >= HEARTBEAT:
            self.last_id_try = now
            self._send_ids(list(self.id_retry), now)


# ---------------------------------------------------------------- VRChat start options: a desktop shortcut (debug logging only works at the START of the game)
def desktop_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for cand in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if cand.exists():
            return cand
    return home / "Desktop"


def sync_launch_shortcut(args: str, folder: Path | None = None) -> bool:
    """Write (or remove, when `args` is empty) "VRChat (Black Box).url": it starts VRChat through Steam with the options the DEV owner chose. The reader cannot change a game
    that is already running, and it never edits Steam's own files. Returns True when something changed."""
    target = (folder or desktop_dir()) / "VRChat (Black Box).url"
    if not args:
        if target.exists():
            target.unlink()
            return True
        return False
    text = f"[InternetShortcut]\nURL=steam://run/438100//{urllib.parse.quote(args, safe='=-,;_.')}/\n"
    if target.exists() and target.read_text("utf-8", errors="replace") == text:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    return True


# ---------------------------------------------------------------- the loop
# ---------------------------------------------------------------- what the window and the logging thread share
class Control:
    """The window (main thread) and the logging loop (worker thread) talk through this: pause / resume, "check for updates", quit, and a status snapshot."""

    def __init__(self, folder: Path):
        self.path = folder / "settings.json"
        self.lock = threading.Lock()
        self.cfg: dict = {}
        try:
            self.cfg = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self.cfg = {}
        self.check_now = threading.Event()
        self.quit = threading.Event()
        self.status: dict = {"version": VERSION, "paused": self.paused, "message": "", "avatar_ids": self.avatar_ids, "ids_allowed": False}

    @property
    def paused(self) -> bool:
        return bool(self.cfg.get("paused"))

    @property
    def hotkey(self) -> str:
        return str(self.cfg.get("hotkey") or "ctrl+shift+n")

    @property
    def avatar_ids(self) -> bool:
        """The person's own switch for reading avatar IDs (default OFF; it only works while the DEV owner has switched it on for everybody as well)."""
        return bool(self.cfg.get("avatar_ids"))

    def set_avatar_ids(self, value: bool) -> None:
        with self.lock:
            self.cfg["avatar_ids"] = bool(value)
            self.status["avatar_ids"] = bool(value)
            self._save()
        log.info("avatar IDs %s by the person", "switched on" if value else "switched off")

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.cfg), "utf-8")
        except OSError:
            log.info("settings not saved", exc_info=True)

    def set_paused(self, value: bool) -> None:
        with self.lock:
            self.cfg["paused"] = bool(value)
            self.status["paused"] = bool(value)
            self._save()
        log.info("logging %s by the person", "paused" if value else "resumed")

    def update(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def ask_for_updates(self) -> None:
        self.update(message="Checking for updates ...")
        self.check_now.set()


# ---------------------------------------------------------------- update (only from our own GitHub releases or from the bot itself, only with a matching SHA-256)
def version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def update_wanted(latest: dict, current: str = VERSION) -> bool:
    url, sha = str(latest.get("url", "")), str(latest.get("sha256", "")).lower()
    source_ok = url.startswith(UPDATE_PREFIXES) or (url == UPDATE_FROM_BOT and all(c in "0123456789." for c in str(latest.get("version", ""))))
    return bool(latest.get("version")) and version_tuple(latest["version"]) > version_tuple(current) and source_ok and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_update(latest: dict, folder: Path, api: Api | None = None) -> Path | None:
    """Fetch the installer and check its SHA-256 against the one the bot announced. Returns the file only when it matches."""
    target = folder / "updates" / "NeglectBlackBox-Setup.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    if latest["url"] == UPDATE_FROM_BOT:
        if api is None or not api.download(f"/bb/v1/update/{latest['version']}", target):
            return None
    else:
        req = urllib.request.Request(latest["url"], headers={"User-Agent": f"NeglectBlackBox/{VERSION}"})  # noqa: S310  # nosec B310 - https GitHub release (update_wanted)
        with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as out:  # noqa: S310  # nosec B310
            if not resp.geturl().startswith("https://"):
                return None
            size = 0
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > INSTALLER_MAX:
                    return None
                out.write(chunk)
    if file_sha256(target) != latest["sha256"].lower():
        target.unlink(missing_ok=True)
        log.warning("update refused: SHA-256 does not match")
        return None
    return target


def fresh_env() -> dict[str, str]:
    """The environment for the installer, which starts the NEW exe: a PyInstaller exe that inherits `_MEIPASS2` / `_PYI_*` from this one reuses this program's temp folder
    (which is deleted when this process ends) and fails with "Failed to load Python DLL ...\\_MEIxxxx\\python313.dll"."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_MEI") or k.startswith("_PYI") or k == "_MEIPASS2")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # PyInstaller 6.9+: "this is a fresh start, not a re-launch of the parent"
    return env


def apply_update(latest: dict, folder: Path, api: Api | None = None) -> bool:
    """Download, check and start the installer (silent; it replaces this program and starts the new one). Ends the whole process when the installer runs."""
    try:
        installer = download_update(latest, folder, api)
    except Exception as e:
        log.info("update download failed: %s", e)
        return False
    if installer is None:
        return False
    log.info("starting the installer of version %s", latest["version"])
    subprocess.Popen([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], creationflags=0x00000008 if sys.platform == "win32" else 0, env=fresh_env())  # noqa: S603
    os._exit(0)  # a thread cannot end the program with sys.exit; the installer closes anything that is left


# ---------------------------------------------------------------- autostart (the installer sets it too; this repairs it)
def ensure_autostart() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "NeglectBlackBox", 0, winreg.REG_SZ, f'"{sys.executable}" run')
    except Exception as e:
        log.info("autostart not set: %s", e)


# ---------------------------------------------------------------- the loop
def vrchat_running() -> bool:
    if sys.platform != "win32":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq VRChat.exe", "/NH"], capture_output=True, text=True, timeout=10, creationflags=0x08000000)  # noqa: S607
        return "VRChat.exe" in out.stdout
    except Exception:
        return True


def first_start(identity: Identity, open_browser=webbrowser.open) -> None:
    """No key yet: create it, ask the bot to list this PC, and open the Discord login once (it proves who sits at this PC)."""
    identity.create(DEFAULT_SERVER)
    Api(identity).register(platform.node() or "PC")
    open_browser(identity.link_url())


def check_in(api: Api, identity: Identity, reader: Reader, state: dict, folder: Path, open_browser=webbrowser.open, control: Control | None = None, manual: bool = False) -> str:
    """Every few minutes (also when no room is open): is this PC linked and approved, is there a new version, is this version still allowed?
    Returns a short sentence for the window (what a click on "Check for updates" shows)."""
    def note(text: str, **kw) -> str:
        if control is not None:
            control.update(**kw, **({"message": text} if manual else {}))
        return text

    try:
        answer = api.hello(reader.loc, reader.parser.debug_on)
    except ApiError as e:
        if e.status in (401, 403):
            log.info("the bot refuses this PC: %s", e.detail)
        return note(f"The bot refused this PC: {e.detail or e.status}", server="refused")
    except Exception as e:
        log.info("check-in failed: %s", e)
        return note("The bot could not be reached", server="down")
    if not answer.get("linked") and not state.get("opened"):
        state["opened"] = True
        open_browser(identity.link_url())  # the person did not finish the Discord login
    if answer.get("min_version") and version_tuple(answer["min_version"]) > version_tuple(VERSION):
        log.warning("this reader (%s) is older than the minimum %s: not sending", VERSION, answer["min_version"])
        reader.track = False
    reader.ids_allowed = bool(answer.get("avatar_ids"))
    if control is not None:
        control.update(ids_allowed=reader.ids_allowed)
    if "launch_args" in answer:
        try:
            sync_launch_shortcut(str(answer["launch_args"] or ""))
        except OSError as e:
            log.info("shortcut not written: %s", e)
    latest = answer.get("latest") or {}
    info = {"server": "ok", "status": answer.get("status"), "linked": bool(answer.get("linked")), "newest": latest.get("version") or ""}
    if update_wanted(latest):
        note(f"Updating to {latest['version']} ...", **info)
        if not apply_update(latest, folder, api):
            return note("The update could not be downloaded or did not pass the check", **info)
        return "updating"
    if latest.get("version") and version_tuple(latest["version"]) > version_tuple(VERSION):
        return note(f"Version {latest['version']} exists but cannot be installed (not verified)", **info)
    return note(f"You have the newest version ({VERSION})", **info)


def run_loop(identity: Identity, opened: bool = False, control: Control | None = None) -> None:
    """`opened`: the Discord login was just opened by the first start (a second tab would overwrite the login cookie and break the first one)."""
    folder = data_dir()
    control = control or Control(folder)
    api = Api(identity)
    reader = Reader(api, identity.cfg["device_id"])
    tail = AmplitudeTail(amplitude_cache_path())
    state: dict = {"opened": opened}
    log_dir = vrchat_log_dir()
    current: Path | None = None
    handle = None
    last_check = last_vr_check = 0.0
    was_running = True
    was_paused = False
    ensure_autostart()
    while not control.quit.is_set():
        try:
            now = time.time()
            if control.paused:  # "Stop logging": nothing is read and nothing is sent until the person resumes
                tail.reset()
                api.ids_state = reader.ids_on = False
                if not was_paused:
                    was_paused = True
                    reader.stop_recording()
                    if handle:
                        handle.close()
                    handle, current = None, None
                time.sleep(1.0)
                if control.check_now.is_set():
                    control.check_now.clear()
                    check_in(api, identity, reader, state, folder, control=control, manual=True)
                control.update(room="", tracked=None, sent=reader.sent)
                continue
            was_paused = False
            latest = newest_log(log_dir) if log_dir.exists() else None
            if latest is not None and latest != current:  # VRChat started (or restarted): the new log
                if handle:
                    handle.close()
                reader.stop_recording()
                current = latest
                handle = open(current, encoding="utf-8", errors="replace")  # noqa: SIM115 - kept open while tailing
                reader.parser = bp.LogParser()
                reader.start_replay(current.name)
                for line in handle:  # what happened before we started: the current room only (older rooms are dropped by the state machine)
                    reader.feed(line)
                reader.end_replay()
            if handle:
                got = False
                for line in handle:
                    reader.feed(line)
                    got = True
                if got:
                    reader.flush()
            reader.heartbeat(now)
            reading = control.avatar_ids and reader.ids_allowed  # both switches: the person's and the DEV owner's, both default OFF
            api.ids_state = reader.ids_on = reading
            if reading:
                reader.feed_ids(tail.poll())
            else:
                tail.reset()  # off: the file is not even opened, and what is in it later is "history", not news
            if control.check_now.is_set():
                control.check_now.clear()
                last_check = now
                check_in(api, identity, reader, state, folder, control=control, manual=True)
            elif now - last_check > CHECK_EVERY:
                last_check = now
                check_in(api, identity, reader, state, folder, control=control)
            if now - last_vr_check > 20:
                last_vr_check = now
                running = vrchat_running()
                if was_running and not running:
                    reader.stop_recording()
                was_running = running
                control.update(vrchat=running)
            control.update(room=reader.parser.world_name if reader.loc else "", tracked=reader.track if reader.loc else None, sent=reader.sent)
        except SystemExit:
            raise
        except Exception:
            log.warning("loop error", exc_info=True)
        time.sleep(1.0)


def selftest(folder: Path) -> int:
    """Run by the build on the finished exe: the parser reads a line, a signature verifies, the data folder is writable."""
    from nacl.signing import SigningKey

    problems = []
    events = bp.parse_lines(["2026.09.30 20:53:02 Debug      -  [Behaviour] Entering Room: X\n", "2026.09.30 20:53:02 Debug      -  [Behaviour] Joining wrld_3c231334-2f20-442f-924c-a504d9f98543:1~region(us)\n"])
    if [e["kind"] for e in events] != ["room_enter"]:
        problems.append("parser")
    key = SigningKey.generate()
    try:
        key.verify_key.verify(key.sign(b"x").message, key.sign(b"x").signature)
    except Exception:
        problems.append("signature")
    if unprotect(protect(b"abc")) != b"abc":
        problems.append("protect")
    if ui is None or ui.parse_hotkey("ctrl+shift+n") != (6, 78):
        problems.append("window module")
    else:
        for name in ("logo.png", "logo.ico"):
            if not (ui.assets_dir() / name).exists():
                problems.append(f"asset {name}")
        if sys.platform == "win32":  # the window only exists there
            try:
                import tkinter

                tkinter.Tcl().eval("info patchlevel")  # the Tcl/Tk part is packed into the exe (no window is opened here)
            except Exception:
                problems.append("tkinter")
    (folder / "selftest.txt").write_text("selftest ok" if not problems else "selftest FAILED: " + ", ".join(problems), "utf-8")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    """A windowless program must never end in an error dialog nobody clicks: every failure goes to crash.txt / reader.log and an exit code."""
    folder = data_dir()
    try:
        return _main(argv, folder)
    except SystemExit:
        raise
    except BaseException:
        import traceback

        try:
            (folder / "crash.txt").write_text(traceback.format_exc(), "utf-8")
        except OSError:
            pass
        return 3


def _main(argv: list[str], folder: Path) -> int:
    handler = RotatingFileHandler(folder / "reader.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    mode = argv[1] if len(argv) > 1 else "run"
    if mode == "selftest":
        return selftest(folder)
    windowed = ui is not None and ui.available() and "--no-window" not in argv
    if ui is not None and not ui.acquire_single_instance():  # the reader already runs: "show" only asks it to open its window
        if mode == "show":
            ui.ask_running_copy_to_show()
        return 0
    identity = Identity(folder)
    opened = False
    if not identity.ready:
        try:
            first_start(identity)
            opened = True
        except Exception:
            log.warning("first start failed", exc_info=True)
            return 2
    control = Control(folder)
    if not windowed or ui is None:
        run_loop(identity, opened, control)
        return 0
    worker = threading.Thread(target=run_loop, args=(identity, opened, control), daemon=True, name="logging")
    worker.start()
    ui.run_window(control, VERSION, show=mode == "show" or opened)  # returns when the person chose "Exit completely"
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
