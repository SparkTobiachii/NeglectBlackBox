"""Neglect Black Box - amplitude probe (read only, nothing is sent anywhere, no network code).

Answers one question: does VRChat's local telemetry file `amplitude.cache` contain the avatar IDs (`avtr_...`) of OTHER players in the same instance,
and when does it show them (live, or only at a world switch)? The answer decides whether exact avatar IDs can ever be attached to players.

What it does: it watches the file (stat only, 4x per second) and, when it changes, reads it and extracts ONLY the `avtr_<uuid>` strings (plain text and
UTF-16). It prints and reports the new IDs, counts of other ID kinds (as NUMBERS only), the size and how the file changes (append / rewrite / cleared).
It never prints, copies or stores any other content of the file, and it never changes or clears the file.

Run (Windows, while VRChat runs):
    python amplitude_probe.py watch --full-ids      watch for 20 minutes (Ctrl+C ends it earlier; `--minutes 40` for longer)
    python amplitude_probe.py once                  one look at the file now
Besides the file it also follows the newest VRChat log (`output_log_*.txt`, read only) and takes ONLY three kinds of lines from it: entering a room (the
world NAME, which sets a marker "(auto)"), leaving a room, `Unpacking Avatar (Name by Creator)` (avatar name + creator), and that somebody joined / left
(only COUNTED, the names are never read, so the report can say how many players the log saw next to a batch of IDs). The report
then shows one timeline of log lines, markers and IDs, so every ID can be compared with the avatars the log says were loaded. `--no-log` turns that off. With VRChat started with the debug option `--enable-sdk-log-levels --log-debug-levels=API` it also notes the lines `[API] Requesting Get avatars/<id>` (live avatar IDs in the log, if VRChat writes them for other players: the test for the "spawn" idea).
Report: `amplitude_probe_report.txt` next to the script (only avtr_ IDs, counts and timestamps - check it before you send it).
IDs are masked by default (`avtr_1a2b3c4d-...`); `--full-ids` shows them whole, which you need to look them up.

SIMPLE TEST (alone, in a PUBLIC instance with a few people):
  1. Start the probe, then VRChat, and join a public world with other players. Stay 3-5 minutes.
  2. Type the number of players you see + Enter (a marker, e.g. `25 players`) - it only helps to compare.
  3. Switch to another world (VRChat may only write the file at a world switch) and wait 1-2 minutes.
  4. Ctrl+C. The end of the report says in one sentence what it found:
       0 IDs      -> the file stays empty or is not written (or the path is wrong)
       1 ID       -> only your own avatar
       2 or more  -> IDs besides your own are collected (whose they are, and which player wears which, this test does NOT tell)

OPTIONAL SECOND TEST (with a helper H and a private instance): type a marker BEFORE each step - `H joined`, then H changes avatar twice,
`H elsewhere` (H changes avatar in ANOTHER instance: nothing new may show up), `world switch`. IDs of H within seconds and nothing in the
negative control means it is usable live; only after the world switch means too late for live use.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

POLL_SECONDS = 0.25
MAX_READ = 16 * 1024 * 1024  # a bigger file is not read at all (the real one is small)
_HEX = r"[0-9a-fA-F]"
_UUID = rf"{_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}"
AVATAR_RE = re.compile(rf"avtr_{_UUID}".encode())
# the same text as UTF-16LE: every character followed by a zero byte
AVATAR_RE16 = re.compile(
    rb"a\x00v\x00t\x00r\x00_\x00(?:[0-9a-fA-F]\x00){8}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){12}")
OTHER_RES = {kind: re.compile(rf"{kind}_{_UUID}".encode()) for kind in ("usr", "wrld", "file", "grp")}


def amplitude_path() -> Path:
    """`%Temp%\\VRChat\\VRChat\\amplitude.cache` (tempfile.gettempdir() is %TEMP% on Windows)."""
    return Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()) / "VRChat" / "VRChat" / "amplitude.cache"


def extract_avatar_ids(data: bytes) -> list[str]:
    """Every distinct `avtr_<uuid>` in the bytes (plain text and UTF-16LE), lower case, in order of first appearance."""
    found: dict[str, None] = {}
    hits = [(m.start(), m.group(0).decode("ascii")) for m in AVATAR_RE.finditer(data)]
    hits += [(m.start(), m.group(0).decode("utf-16-le")) for m in AVATAR_RE16.finditer(data)]
    for _, text in sorted(hits):
        found.setdefault(text.lower(), None)
    return list(found)


def extract_avatar_ids_ordered(data: bytes) -> list[str]:
    """Every `avtr_<uuid>` in the order it stands in the file, DUPLICATES kept (the position in the file may mean something)."""
    hits = [(m.start(), m.group(0).decode("ascii")) for m in AVATAR_RE.finditer(data)]
    hits += [(m.start(), m.group(0).decode("utf-16-le")) for m in AVATAR_RE16.finditer(data)]
    return [text.lower() for _, text in sorted(hits)]


def count_other_ids(data: bytes) -> dict[str, int]:
    """How many distinct usr_ / wrld_ / file_ / grp_ IDs are in the file - only the NUMBERS, never the IDs."""
    return {kind: len({m.group(0).lower() for m in rx.finditer(data)}) for kind, rx in OTHER_RES.items()}


def detect_encoding(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "UTF-16 (BOM)"
    sample = data[:4096]
    if sample and sample.count(b"\x00") > len(sample) // 3:
        return "UTF-16 (no BOM)"
    return "ASCII / UTF-8"


def mask(avatar_id: str, full: bool) -> str:
    return avatar_id if full else avatar_id[:13] + "-..."


def change_kind(old_size: int, new_size: int) -> str:
    """How the file changed: appended, rewritten (same size or a different content of similar size), or cleared."""
    if new_size == 0:
        return "cleared"
    if new_size > old_size:
        return "grew"
    if new_size < old_size:
        return "shrank"
    return "rewritten (same size)"


def parse_log_line(line: str) -> tuple[float, str, str] | None:
    """(epoch, kind, text) for the three kinds of VRChat log lines the probe cares about, else None.
    kind: room (text = world name), leave, unpack (text = `Avatar name by Creator`), pjoin / pleft (somebody joined / left, text empty). Player names are never read."""
    try:
        epoch = time.mktime(time.strptime(line[:19], "%Y.%m.%d %H:%M:%S"))
    except ValueError:
        return None
    if "[Behaviour] Entering Room: " in line:
        return epoch, "room", line.split("Entering Room: ", 1)[1].strip()
    if "[Behaviour] OnLeftRoom" in line:
        return epoch, "leave", ""
    if "[Behaviour] OnPlayerJoined " in line:
        return epoch, "pjoin", ""  # the name is NOT taken
    if "[Behaviour] OnPlayerLeft " in line:
        return epoch, "pleft", ""
    if "[API] Requesting Get avatars/" in line:  # only with the debug log (VRChat started with --log-debug-levels=API)
        found = AVATAR_RE.search(line.encode("utf-8", "replace"))
        return (epoch, "api_avatar", found.group(0).decode("ascii").lower()) if found else None
    if "[AssetBundleDownloadManager]" in line and "Unpacking Avatar (" in line:
        text = line.split("Unpacking Avatar (", 1)[1].rstrip()
        return epoch, "unpack", text[:-1] if text.endswith(")") else text
    return None


def log_folder() -> Path:
    """`%LOCALAPPDATA%\\..\\LocalLow\\VRChat\\VRChat` (where output_log_*.txt lives)."""
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local).parent / "LocalLow" / "VRChat" / "VRChat") if local else Path.home() / "AppData" / "LocalLow" / "VRChat" / "VRChat"


class LogTail:
    """Follows the newest output_log_*.txt by offset and yields only the parsed lines of `parse_log_line`.
    A log that already exists when the probe starts is followed from its END; a log that appears later (VRChat started after the probe) from its start."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.file: Path | None = None
        self.offset = 0
        self.rest = b""
        self.started = False

    def newest(self) -> Path | None:
        try:
            return max(self.folder.glob("output_log_*.txt"), key=lambda f: f.stat().st_mtime, default=None)
        except OSError:
            return None

    def poll(self) -> list[tuple[float, str, str]]:
        newest = self.newest()
        if newest is None:
            self.started = True
            return []
        try:
            size = newest.stat().st_size
            if newest != self.file:
                self.file, self.rest = newest, b""
                self.offset = size if not self.started else 0
                self.started = True
                if self.offset == size:
                    return []
            if size < self.offset:
                self.offset, self.rest = 0, b""
            if size == self.offset:
                return []
            with open(newest, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(min(size - self.offset, 4 * 1024 * 1024))
        except OSError:
            return []
        self.offset += len(chunk)
        *lines, self.rest = (self.rest + chunk).split(b"\n")
        events = (parse_log_line(raw.decode("utf-8", "replace")) for raw in lines)
        return [e for e in events if e]


class Probe:
    """The state: what IDs were in the previous look, which are new, grouped by marker. Pure logic, the file access is in `look`."""

    def __init__(self, full_ids: bool = False, clock=time.time):
        self.full = full_ids
        self.clock = clock
        self.t0 = clock()
        self.previous: set[str] = set()  # IDs in the previous snapshot
        self.ever: dict[str, float] = {}  # id -> first time seen
        self.size = -1
        self.mtime = -1.0
        self.groups: list[dict] = [{"label": "(before the first marker)", "at": self.t0, "new": [], "again": []}]
        self.changes: dict[str, int] = {}
        self.locked = 0
        self.encoding = ""
        self.others: dict[str, int] = {}
        self.lines: list[str] = []
        self.timeline: list[tuple[float, str]] = []  # (epoch, text): markers, IDs seen in the file, log lines
        self.room_players = 0  # joins minus leaves counted in the log since the room was entered (numbers only)
        self.api_ids = 0  # `[API] Requesting Get avatars/<id>` lines seen in the log (only with the debug log)
        self.biggest: list[str] = []  # the ordered ID list (with duplicates) of the snapshot with the most entries

    def log_event(self, epoch: float, kind: str, text: str) -> None:
        """A parsed log line: a room change sets an automatic marker, an unpacked avatar goes into the timeline."""
        if kind == "pjoin":
            self.room_players += 1
        elif kind == "pleft":
            self.room_players = max(0, self.room_players - 1)
        elif kind == "room":
            self.room_players = 0
            self.marker(f"(auto) entered room: {text[:60]}")
        elif kind == "leave":
            self.marker("(auto) left room")
        elif kind == "api_avatar":
            self.api_ids += 1
            self.timeline.append((epoch, f"log   API asked for avatar {mask(text, self.full)}"))
            self.log(f"log    API asked for avatar {mask(text, self.full)}")
        elif kind == "unpack":
            self.timeline.append((epoch, f"log   avatar loaded: {text[:90]}"))
            self.log(f"log    avatar loaded: {text[:90]}")

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        line = f"{stamp}  {text}"
        self.lines.append(line)
        print(line, flush=True)

    def marker(self, label: str) -> None:
        self.groups.append({"label": label.strip()[:80] or "marker", "at": self.clock(), "new": [], "again": []})
        self.timeline.append((self.clock(), f"----  marker: {self.groups[-1]['label']}"))
        self.log(f"---- marker: {self.groups[-1]['label']} ----")

    def feed(self, data: bytes, size: int, mtime: float) -> None:
        """One look at the file content: ids that were not in the previous look are NEW (a first appearance) or AGAIN (seen before in this run)."""
        kind = change_kind(self.size, size) if self.size >= 0 else "first look"
        self.changes[kind] = self.changes.get(kind, 0) + 1
        self.size, self.mtime = size, mtime
        self.encoding = detect_encoding(data) if data else self.encoding
        ordered = extract_avatar_ids_ordered(data)
        if len(ordered) > len(self.biggest):
            self.biggest = ordered
        ids = extract_avatar_ids(data)
        now = self.clock()
        fresh = [i for i in ids if i not in self.previous]
        self.previous = set(ids)
        self.others = count_other_ids(data)
        group = self.groups[-1]
        for i in fresh:
            since = now - group["at"]
            if i in self.ever:
                group["again"].append((i, round(since, 1)))
                self.timeline.append((now, f"ID    again {mask(i, self.full)}"))
                self.log(f"again  {mask(i, self.full)}  (+{since:.1f}s after the marker, first seen {now - self.ever[i]:.0f}s ago)")
            else:
                self.ever[i] = now
                group["new"].append((i, round(since, 1)))
                self.timeline.append((now, f"ID    NEW   {mask(i, self.full)}"))
                self.log(f"NEW    {mask(i, self.full)}  (+{since:.1f}s after the marker)")
        if len(fresh) >= 5:
            note = f"batch of {len(fresh)} IDs ({len(ordered)} entries in the file); players counted in the log since entering the room: {self.room_players}"
            self.timeline.append((now, "NOTE  " + note))
            self.log(note)
        if not fresh:
            self.log(f"file {kind}: {size} bytes, {len(ids)} avatar ID(s) in it, nothing new")

    def look(self, path: Path) -> bool:
        """Stat the file; if it changed, read it (never more than MAX_READ) and feed it. False when there was nothing to look at."""
        try:
            st = path.stat()
        except OSError:
            if self.size != -2:
                self.size = -2
                self.log("the file does not exist (VRChat not started yet?)")
            return False
        if (st.st_size, st.st_mtime) == (self.size, self.mtime):
            return True
        if st.st_size > MAX_READ:
            self.log(f"file is {st.st_size} bytes - too big, not read")
            self.size, self.mtime = st.st_size, st.st_mtime
            return True
        try:
            data = path.read_bytes()
        except OSError:
            self.locked += 1  # VRChat holds it right now: the next poll tries again
            return True
        self.feed(data, st.st_size, st.st_mtime)
        return True

    def verdict(self) -> str:
        n = len(self.ever)
        if n == 0:
            return "no avatar ID seen - the file stays empty or is not written yet (try a world switch and a longer run, or the path is wrong)."
        if n == 1:
            return "1 avatar ID - most likely only your own avatar."
        return (f"{n} different avatar IDs - more than your own seem to be collected. This does NOT say whose they are or which player wears which "
                "(compare with the player count and the timing of the markers).")

    def report(self, path: Path) -> str:
        out = ["Neglect Black Box amplitude probe", "contains only avtr_ IDs, counts and timestamps - check it before you send it", ""]
        out.append(f"file: {path}")
        out.append(f"watched for {self.clock() - self.t0:.0f} s, last size {max(self.size, 0)} bytes, encoding {self.encoding or 'unknown'}, locked reads {self.locked}")
        out.append("how the file changed: " + (", ".join(f"{k} x{n}" for k, n in sorted(self.changes.items())) or "no look at all"))
        out.append("other ID kinds in the last snapshot (numbers only): " + ", ".join(f"{k} {n}" for k, n in self.others.items()))
        out.append(f"distinct avatar IDs seen: {len(self.ever)}")
        out.append(f"API avatar request lines in the log (needs VRChat started with --log-debug-levels=API): {self.api_ids}")
        out.append("verdict: " + self.verdict())
        for g in self.groups:
            if not g["new"] and not g["again"] and g is self.groups[0]:
                continue
            out.append("")
            out.append(f"[{time.strftime('%H:%M:%S', time.localtime(g['at']))}] {g['label']}: {len(g['new'])} new, {len(g['again'])} seen again")
            for i, s in g["new"]:
                out.append(f"    NEW   +{s}s  {mask(i, self.full)}")
            for i, s in g["again"]:
                out.append(f"    again +{s}s  {mask(i, self.full)}")
        if self.biggest:
            seen: dict[str, int] = {}
            lines = []
            for n, i in enumerate(self.biggest, 1):
                lines.append(f"{n:>4}  {mask(i, self.full)}" + (f"   (same as #{seen[i]})" if i in seen else ""))
                seen.setdefault(i, n)
            out += ["", f"ORDER IN THE FILE (the snapshot with the most entries: {len(self.biggest)} entries, {len(seen)} different)", *lines]
        if self.timeline:
            out += ["", "TIMELINE (ID = when the probe saw it in the file; log = time of the VRChat log line; marker = your note or an automatic room change)"]
            out += [f"{time.strftime('%H:%M:%S', time.localtime(t))}  {text}" for t, text in sorted(self.timeline, key=lambda e: e[0])]
        return "\n".join(out)


def read_markers(probe: Probe, stop: threading.Event) -> None:
    """Every line typed in the console is a marker. Ends silently when there is no console."""
    while not stop.is_set():
        try:
            text = input()
        except (EOFError, OSError):
            return
        probe.marker(text)


def run_once(path: Path, full: bool) -> int:
    probe = Probe(full)
    if not probe.look(path):
        print(f"No file at {path}. Start VRChat once, then run this again.")
        return 1
    print(probe.report(path))
    return 0


def run_watch(path: Path, minutes: float, full: bool, follow_log: bool = True) -> int:
    probe = Probe(full)
    tail = LogTail(log_folder()) if follow_log else None
    print(f"Watching {path} for {minutes:g} minutes. Join a public instance, stay a few minutes, then switch the world once.")
    print("Optional: type a note (e.g. the player count) + Enter to set a marker. Ctrl+C ends it and writes the report.\n")
    stop = threading.Event()
    threading.Thread(target=read_markers, args=(probe, stop), daemon=True).start()
    end = time.time() + minutes * 60
    try:
        while time.time() < end:
            probe.look(path)
            if tail:
                for epoch, kind, text in tail.poll():
                    probe.log_event(epoch, kind, text)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped.")
    stop.set()
    text = probe.report(path)
    target = Path(__file__).with_name("amplitude_probe_report.txt")
    target.write_text(text, "utf-8")
    print("\n" + text)
    print(f"\nReport written to {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only probe of VRChat's amplitude.cache: which avtr_ IDs show up, and when.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("once", help="look at the file once")
    watch = sub.add_parser("watch", help="watch the file and group new IDs by marker")
    for p in (once, watch):
        p.add_argument("--full-ids", action="store_true", help="print avatar IDs whole instead of masked")
        p.add_argument("--file", type=Path, help="another path than %%TEMP%%\\VRChat\\VRChat\\amplitude.cache")
    watch.add_argument("--minutes", type=float, default=20)
    watch.add_argument("--no-log", action="store_true", help="do not follow the VRChat log (no automatic room markers, no avatar lines)")
    args = ap.parse_args(argv)
    path = args.file or amplitude_path()
    if args.cmd == "once":
        return run_once(path, args.full_ids)
    return run_watch(path, args.minutes, args.full_ids, not args.no_log)


if __name__ == "__main__":
    sys.exit(main())
