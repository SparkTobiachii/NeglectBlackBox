"""Black Box: tolerant parser for the VRChat `output_log_*.txt` (pure standard library, NO neglect imports).

The same file is used by the Windows reader (client/blackbox_reader) and by the bot (tests, server-side checks), so the reader sends compact events and never
the raw log. Everything unknown is dropped: the world scripts (Just B Club ...) change, so every pattern is optional and a miss never raises.

Names in the log are NOT unique (look-alike characters): every event that has a usr_ ID carries it, and the server resolves people by that ID.

Event = {"ts": unix seconds, "kind": str, "loc": location or "", "data": {...}}. Kinds:
  room_enter   data: world, world_name, instance, access, region, group_id, owner      (the line `Joining wrld_...`)
  room_leave   data: {}                                                                (OnLeftRoom)
  join         data: usr, name, roster (bool: already in the room when the reader arrived), local (bool: the person who wears this log)
  leave        data: usr, name
  avatar       data: usr, name, avatar (only the avatar NAME is in the log, never an avtr_ ID for other people)
  master       data: name, usr
  role         data: usr, name, role, on (bool), by (name of who applied it, may be ""), state (bool: the roles read when the person arrived, not a change)
  notify       data: text, by                                                          (`_Notification(Access role, Added by X, ...)`, Just B Club)
  host         data: name                                                              ([MyInstance] Setting Host to X)
  vip_host     data: name                                                              ([MyInstance] Setting VIPHost to X)
  vip_activate data: {}                                                                ([MyInstance] ActivateVipInstance)
  vip_purchase data: name                                                              (OnPurchaseConfirmed - but X ...)
  vip_guest    data: name, usr, on (bool)                                              (`Whitelist changed from 2|1,6|1 to 2|1`: the VIP guest list of Just B Club; entries are `<Photon actor number>|<flag>`)
  announce     data: name, usr, text                                                   (`_Notification(Announcement by X, "text", RGBA(`: a text announcement in Just B Club, with its content; a text with line breaks arrives on several lines)
  video        data: url, by (who most likely started it: the last person who got the Video role in the 3 minutes before, may be "")  (`[Video Playback] Attempting to resolve URL '...'`: the log does not say WHO pressed play)
  suite        data: name, usr, room, text                                              (private room of Just B Club: `[Rooms] _EnterRoomParam(2)` = went in; the settings come as `_Notification(Intercom disabled, ...)`, `Invite icon enabled`, `Occupant names hidden`, `Suite Locked`)
  security     data: on (bool)                                                         ([Players] OnSecurityEnabledChanged(True))
  avatar_load  data: avatar, author                                                   (`[AssetBundleDownloadManager] [n] Unpacking Avatar (Name by Author)`: an avatar was loaded, with its creator; no usr_ in the line)
  room_state   data: room, clogged (bool)                                              ([Room] Bedroom 3 OnDeserialization - IsClogged = True, only when it changed)
  avatar_id    data: id (avtr_...)                                                     (the reader takes it from VRChat's amplitude.cache with `avatar_ids_from` and - with the debug log - from the line `[API] Requesting Get avatars/<id>`; the reader sends it only when the person and the DEV owner both switched avatar IDs on)
"""

from __future__ import annotations

import re
from datetime import datetime

AVTR_RE = re.compile(r"^avtr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # the form every avatar ID must have (lower case) before it is used or stored
_AVTR_FIND = re.compile(rb"avtr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_AVTR_FIND16 = re.compile(  # the same text as UTF-16LE: every character followed by a zero byte
    rb"a\x00v\x00t\x00r\x00_\x00(?:[0-9a-fA-F]\x00){8}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){4}-\x00(?:[0-9a-fA-F]\x00){12}")
AVATAR_IDS_MAX = 400  # one look at the file never yields more than this
LINE_TS = re.compile(r"^(\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d) ")
USR = r"(usr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
RE_AUTH = re.compile(r"User Authenticated: (.+) \(" + USR + r"\)\s*$")
RE_ENTER = re.compile(r"\[Behaviour\] Entering Room: (.+?)\s*$")
RE_JOINING = re.compile(r"\[Behaviour\] Joining (wrld_[0-9a-fA-F-]{36}:\S+)\s*$")
RE_LEFT_ROOM = re.compile(r"\[Behaviour\] OnLeftRoom\s*$")
RE_PJOIN = re.compile(r"\[Behaviour\] OnPlayerJoined (.+) \(" + USR + r"\)\s*$")
RE_PLEAVE = re.compile(r"\[Behaviour\] OnPlayerLeft (.+) \(" + USR + r"\)\s*$")
RE_AVATAR = re.compile(r"\[Behaviour\] Switching (.+?) to avatar (.+?)\s*$")
RE_MASTER = re.compile(r"\[Players\] UpdateInstanceMaster - newMaster = (.+?)\s*$")
RE_ROLES_CALL = re.compile(r"\[players _UpdatePlayer\((.*), (\d+), (\d+), (.*?)\s*$")
RE_ROLE = re.compile(r"\[Players\._UpdatePlayer\] (\w+) - prevOn = (True|False) - nextOn = (True|False), byPlayer = (.+?)\s*$")
RE_ANNOUNCE = re.compile(r"_Notification\(Announcement by (.+?), \"(.*)\", RGBA\(", re.S)
RE_VIDEO = re.compile(r"\[Video Playback\] Attempting to resolve URL '([^']+)'")
RE_NOTIFY = re.compile(r"_Notification\((\w[\w ]* role), (Added|Removed) by (.+?), RGBA\(")
RE_HOST = re.compile(r"\[MyInstance\] Setting Host to (.+?)\s*$")
RE_VIPHOST = re.compile(r"\[MyInstance\] Setting VIPHost to (.+?)\s*$")
RE_VIP = re.compile(r"\[MyInstance\] ActivateVipInstance\s*$")
RE_PURCHASE = re.compile(r"OnPurchaseConfirmed - but (.+?) doesn't have")
RE_SECURITY = re.compile(r"\[Players\] OnSecurityEnabledChanged\((True|False)\)")
RE_API_AVATAR = re.compile(r"\[API\] Requesting Get avatars/(avtr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
RE_UNPACK = re.compile(r"\[AssetBundleDownloadManager\] \[\d+\] Unpacking Avatar \((.+)\)\s*$")
RE_POSTLOGIN = re.compile(r"post login checks for (.+) \(" + USR + r"\)\s*$")  # newer VRChat versions: no `User Authenticated` line any more
RE_CONFIG = re.compile(r"\[Behaviour\] Configuring (local|remote) player VRCPlayer\[\w+\] \d+ (\d+)\s*$")
RE_LOADAV = re.compile(r"\[Behaviour\] Loading avatar for (.+?)\s*$")
RE_TOGGLE_P = re.compile(r"\[Player\] _ToggleRole\((\d+), (.+?)\s*$")  # somebody toggled a role of the person who wears this log: the 2nd argument is who
RE_TOGGLE_UI = re.compile(r"\[Players\] _ToggleRole\((\d+), (\d+)\)")  # the person who wears this log toggled a role of somebody else in the panel
RE_WHITELIST = re.compile(r"Whitelist changed from (.*?) to (.*?)\s*$")
RE_ENTER_SUITE = re.compile(r"\[Rooms\] _EnterRoomParam\((\d+)\)")
RE_ROOMDIST = re.compile(r"_GetRoom dist from (Bedroom(?: VIP)? \d+) = ([\d.]+)")
RE_SUITE_NOTE = re.compile(r"_Notification\(((?:Intercom|Invite icon|Occupant names|Suite|Do not disturb|Action denied)[^,]*), ?([^,]*), RGBA\(")
RE_ARG = re.compile(r"\sArg: (.+?)\s*$")
DEBUG_ARGS = ("--log-debug-levels", "--enable-sdk-log-levels", "--enable-verbose-logging")
RE_CLOG = re.compile(r"\[Room\] (Bedroom(?: VIP)? \d+) OnDeserialization - IsClogged = (True|False)")

# the roles the Just B Club world logs; a name nobody knows is still reported
ROLES = ("Host", "Security", "Bartender", "Performer", "Video", "Access")
TOGGLE_WINDOW = 4.0  # seconds a `_ToggleRole` line stays valid for the role lines that follow it
VIDEO_WINDOW = 180.0  # seconds after somebody got the Video role in which a video start is put on that person
STATE_WINDOW = 20.0  # seconds after a person arrives in which their role lines are the roles they already had, not a change


def parse_line_ts(line: str, tz=None) -> float | None:
    """`2026.09.30 20:52:54 ...` -> unix seconds (the log has local time without a zone; `tz` None = this PC's zone)."""
    m = LINE_TS.match(line)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()), tzinfo=tz).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_location(loc: str) -> dict:
    """`wrld_x:12345~group(grp_y)~groupAccessType(public)~region(eu)` -> world, instance, access, region, group_id, owner."""
    world, _, rest = (loc or "").partition(":")
    instance, *flags = rest.split("~")
    info = {"world": world, "instance": instance, "access": "public", "region": "us", "group_id": "", "owner": "", "raw": loc or ""}
    group_type = ""
    for f in flags:
        key, _, val = f.partition("(")
        val = val.rstrip(")")
        if key == "region":
            info["region"] = val
        elif key == "group":
            info["group_id"] = val
        elif key == "groupAccessType":
            group_type = val
        elif key in ("private", "friends", "hidden"):
            info["owner"] = val
            info["access"] = {"private": "invite", "friends": "friends", "hidden": "friends+"}[key]
            if key == "private" and any(x.startswith("canRequestInvite") for x in flags):
                info["access"] = "invite+"
    if info["group_id"]:
        info["access"] = {"public": "group public", "plus": "group+", "members": "group"}.get(group_type, "group")
    return info


class LogParser:
    """Feed it the lines of ONE log file in order; `feed(line)` returns the events that line produced (usually none)."""

    def __init__(self, tz=None):
        self.tz = tz
        self.local_usr = ""
        self.local_name = ""
        self.loc = ""
        self.world_name = ""
        self.in_room = False
        self.roster = False  # between `Joining` and the local player's own OnPlayerJoined
        self.present: dict[str, str] = {}  # name -> usr_ of the people in the room
        self.joined_at: dict[str, float] = {}  # usr -> time the join line was seen
        self.pending_avatar: dict[str, tuple[float, str]] = {}  # name -> (ts, avatar) for people whose join line did not come yet
        self.pending_roles: dict[str, list[dict]] = {}  # name -> role events seen before that person's join line (the log lists roles first)
        self.role_actor: dict[str, str] = {}  # name -> who applied the roles (from the `[players _UpdatePlayer(...)` line right before the role lines)
        self.clogged: dict[str, bool] = {}
        self.actors: dict[int, str] = {}  # Photon actor number -> name (the VIP guest list uses the numbers)
        self.cfg: tuple[bool, int] | None = None  # the `Configuring ... player` line waiting for its `Loading avatar for <name>` line
        self.toggle: tuple[float, str] = (0.0, "")  # (time, who) of the last `_ToggleRole` line
        self.suite_room = ""  # the private room the person who wears this log is in ("Bedroom 2")
        self.dists: dict[str, float] = {}  # `_GetRoom dist from Bedroom N = x` lines since the last `Setting currentRoom`: the nearest room is the one
        self.video_giver: tuple[float, str, str] = (-999.0, "", "")  # (time, name, usr) of the last Video role that was really given
        self.last_video = ("", -999.0)
        self.ann: tuple[float, str] | None = None  # an announcement whose text runs over several lines: (time, text so far)
        self.recent_roles: dict[tuple[str, str, bool], float] = {}  # (name, role, on) -> time of the role event, so the matching `_Notification` is not a second line
        self.last_ts = 0.0
        self.launch_args: list[str] = []  # how this VRChat was started (`Launching with args` + `Arg:` lines at the top of the log)

    @property
    def debug_on(self) -> bool:
        """Was VRChat started with a debug / verbose logging option?"""
        return any(a.startswith(DEBUG_ARGS) for a in self.launch_args)

    def reset_room(self) -> None:
        self.in_room = False
        self.roster = False
        self.present.clear()
        self.joined_at.clear()
        self.pending_avatar.clear()
        self.pending_roles.clear()
        self.role_actor.clear()
        self.clogged.clear()
        self.actors.clear()
        self.cfg = None
        self.toggle = (0.0, "")
        self.recent_roles.clear()
        self.video_giver = (-999.0, "", "")
        self.last_video = ("", -999.0)
        self.ann = None
        self.suite_room = ""
        self.dists.clear()

    def _ev(self, ts: float, kind: str, **data) -> dict:
        return {"ts": ts, "kind": kind, "loc": self.loc, "data": data}

    def feed(self, line: str) -> list[dict]:
        try:
            return self._feed(line)
        except Exception:  # a log is a moving target: one odd line must never stop the reader
            return []

    def _feed(self, line: str) -> list[dict]:
        ts = parse_line_ts(line, self.tz)
        if ts is None:
            if self.ann is not None:  # the rest of a multi-line announcement
                at, text = self.ann
                text += "\n" + line.rstrip("\r\n")
                if "RGBA(" in line:
                    self.ann = None
                    return self._announce(at, text)
                self.ann = (at, text) if len(text) < 2000 else None
            return []
        self.ann = None  # a new log line: whatever was open is over
        self.last_ts = ts
        # the cheap pre-filter: most of the log is noise, and these checks are substring tests
        if "Launching with args" in line:
            self.launch_args = []
            return []
        if " Arg: " in line:
            m = RE_ARG.search(line)
            if m:
                self.launch_args.append(m.group(1))
            return []
        if "[Behaviour]" in line:
            return self._behaviour(ts, line)
        if "Requesting Get avatars/" in line:  # only with the debug log (VRChat started with --log-debug-levels=API); the format is taken from reports of other tools, not seen here yet
            m = RE_API_AVATAR.search(line)
            return [self._ev(ts, "avatar_id", id=m.group(1).lower())] if m and self.in_room else []
        if "Unpacking Avatar" in line:
            m = RE_UNPACK.search(line)
            if m and self.in_room:
                avatar, _, author = m.group(1).rpartition(" by ")  # "Vyper by Strawbunny Medium by Melting3D": the creator is after the LAST " by "
                if avatar:
                    return [self._ev(ts, "avatar_load", avatar=avatar.strip(), author=author.strip())]
            return []
        if "post login checks for" in line:
            m = RE_POSTLOGIN.search(line)
            if m:
                self.local_name, self.local_usr = m.group(1), m.group(2)
            return []
        if "User Authenticated" in line:
            m = RE_AUTH.search(line)
            if m:
                self.local_name, self.local_usr = m.group(1), m.group(2)
            return []
        if not self.in_room:
            return []
        if "[Video Playback] Attempting" in line:
            m = RE_VIDEO.search(line)
            local_joined = self.joined_at.get(self.local_usr)
            if not m or self.roster or (local_joined is not None and ts - local_joined <= STATE_WINDOW):
                return []  # what was already playing when the log wearer arrived
            url = re.sub(r"[?&]si=[^&]*", "", m.group(1))[:200]
            if self.last_video[0] == url and ts - self.last_video[1] < 10:
                return []
            self.last_video = (url, ts)
            at, name, _usr = self.video_giver
            by = name if ts - at <= VIDEO_WINDOW else ""
            return [self._ev(ts, "video", url=url, by=by)]
        if "UpdateInstanceMaster" in line:
            m = RE_MASTER.search(line)
            if m:
                return [self._ev(ts, "master", name=m.group(1), usr=self.present.get(m.group(1), ""))]
        elif "[players _UpdatePlayer(" in line:
            m = RE_ROLES_CALL.search(line)
            if m:
                self.role_actor[m.group(1)] = m.group(4)
        elif "[Players._UpdatePlayer]" in line:
            return self._role(ts, line)
        elif "_ToggleRole(" in line:
            m = RE_TOGGLE_P.search(line)
            if m:
                self.toggle = (ts, m.group(2))
            elif RE_TOGGLE_UI.search(line):
                self.toggle = (ts, self.local_name)
        elif "Whitelist changed from" in line:
            return self._whitelist(ts, line)
        elif "_EnterRoomParam(" in line:
            m = RE_ENTER_SUITE.search(line)
            if m:
                n = m.group(1)
                self.suite_room = next((r for r in self.clogged if r.endswith(" " + n)), f"Bedroom {n}")
                return [self._ev(ts, "suite", name=self.local_name, usr=self.local_usr, room=self.suite_room, text="entered")]
        elif "_GetRoom dist from" in line:
            m = RE_ROOMDIST.search(line)
            if m:
                self.dists[m.group(1)] = float(m.group(2))
        elif "Setting currentRoom to this room" in line:
            if self.dists:
                self.suite_room = min(self.dists, key=lambda r: self.dists[r])
                self.dists.clear()
        elif "_Notification(" in line:
            if "_Notification(Announcement by " in line:
                if "RGBA(" in line:
                    return self._announce(ts, line)
                self.ann = (ts, line)
                return []
            sm = RE_SUITE_NOTE.search(line)
            if sm:
                return [self._ev(ts, "suite", name=self.local_name, usr=self.local_usr, room=self.suite_room, text=(sm.group(1).strip() + (": " + sm.group(2).strip() if sm.group(1).startswith("Action denied") and sm.group(2).strip() else "")))]
            m = RE_NOTIFY.search(line)
            if m:
                role = m.group(1).removesuffix(" role")
                if ts - self.recent_roles.get((m.group(3), role, m.group(2) == "Added"), -999.0) <= TOGGLE_WINDOW:
                    return []  # the role line right before it already said this
                return [self._ev(ts, "notify", text=f"{m.group(1)} {m.group(2).lower()}", by=m.group(3), role=m.group(1), added=m.group(2) == "Added")]
        elif "[MyInstance]" in line:
            for rx, kind in ((RE_VIPHOST, "vip_host"), (RE_HOST, "host")):
                m = rx.search(line)
                if m:
                    return [self._ev(ts, kind, name=m.group(1), usr=self.present.get(m.group(1), ""))]
            if RE_VIP.search(line):
                return [self._ev(ts, "vip_activate")]
        elif "OnPurchaseConfirmed" in line:
            m = RE_PURCHASE.search(line)
            if m:
                return [self._ev(ts, "vip_purchase", name=m.group(1), usr=self.present.get(m.group(1), ""))]
        elif "OnSecurityEnabledChanged" in line:
            m = RE_SECURITY.search(line)
            if m:
                return [self._ev(ts, "security", on=m.group(1) == "True")]
        elif "IsClogged" in line:
            m = RE_CLOG.search(line)
            if m:
                room, state = m.group(1), m.group(2) == "True"
                if self.clogged.get(room) is not state:
                    first = room not in self.clogged
                    self.clogged[room] = state
                    if not (first and not state):  # the first "free" of a room is no news
                        return [self._ev(ts, "room_state", room=room, clogged=state)]
        return []

    def _behaviour(self, ts: float, line: str) -> list[dict]:
        out: list[dict] = []
        if "Configuring " in line:
            m = RE_CONFIG.search(line)
            if m and self.in_room:
                self.cfg = (m.group(1) == "local", int(m.group(2)))
            return out
        if "Loading avatar for " in line:
            m = RE_LOADAV.search(line)
            if m and self.cfg and self.in_room:
                is_local, actor = self.cfg
                self.actors[actor] = m.group(1)
                if is_local:
                    self.local_name = m.group(1)  # the name is certain here even when no login line told it
            self.cfg = None
            return out
        if "OnPlayerJoined " in line:
            m = RE_PJOIN.search(line)
            if not m or not self.in_room:
                return out
            name, usr = m.group(1), m.group(2)
            local = usr == self.local_usr or (bool(self.local_name) and name == self.local_name)
            if local:
                self.local_usr = usr
            roster = self.roster and not local
            self.present[name] = usr
            self.joined_at[usr] = ts
            out.append(self._ev(ts, "join", usr=usr, name=name, roster=roster, local=local))
            if local:
                self.roster = False
            if name in self.pending_avatar:
                at, avatar = self.pending_avatar.pop(name)
                out.append(self._ev(at, "avatar", usr=usr, name=name, avatar=avatar))
            for ev in self.pending_roles.pop(name, []):
                ev["data"]["usr"] = usr
                out.append(ev)
            return out
        if "OnPlayerLeft " in line:
            m = RE_PLEAVE.search(line)
            if m and self.in_room:
                name, usr = m.group(1), m.group(2)
                self.present.pop(name, None)
                self.joined_at.pop(usr, None)
                out.append(self._ev(ts, "leave", usr=usr, name=name))
            return out
        if "Switching " in line:
            m = RE_AVATAR.search(line)
            if m and self.in_room:
                name, avatar = self._split_avatar(m.group(0))
                usr = self.present.get(name)
                if usr:
                    out.append(self._ev(ts, "avatar", usr=usr, name=name, avatar=avatar))
                else:
                    self.pending_avatar[name] = (ts, avatar)
            return out
        if "Entering Room:" in line:
            m = RE_ENTER.search(line)
            if m:
                if self.in_room:
                    out.append(self._ev(ts, "room_leave"))
                self.reset_room()
                self.world_name = m.group(1)
                self.loc = ""
            return out
        if "Joining wrld_" in line:
            m = RE_JOINING.search(line)
            if m:
                self.loc = m.group(1)
                self.in_room = True
                self.roster = True
                info = parse_location(self.loc)
                out.append(self._ev(ts, "room_enter", world=info["world"], world_name=self.world_name, instance=info["instance"], access=info["access"],
                                    region=info["region"], group_id=info["group_id"], owner=info["owner"]))
            return out
        if RE_LEFT_ROOM.search(line):
            if self.in_room:
                out.append(self._ev(ts, "room_leave"))
            self.reset_room()
            self.loc = ""
        return out

    def _announce(self, ts: float, text: str) -> list[dict]:
        m = RE_ANNOUNCE.search(text)
        if not m:
            return []
        name = m.group(1).strip()
        return [self._ev(ts, "announce", name=name, usr=self.present.get(name, self.local_usr if name == self.local_name else ""), text=m.group(2).strip()[:300])]

    def _split_avatar(self, tail: str) -> tuple[str, str]:
        """`[Behaviour] Switching <name> to avatar <avatar>`: a name or an avatar may itself contain " to avatar ", so a name we already know wins."""
        body = tail.split("Switching ", 1)[1]
        for name in sorted(self.present, key=len, reverse=True):
            if body.startswith(name + " to avatar "):
                return name, body[len(name) + len(" to avatar "):].strip()
        name, _, avatar = body.partition(" to avatar ")
        return name, avatar.strip()

    def _whitelist(self, ts: float, line: str) -> list[dict]:
        """The VIP guest list of Just B Club: `Whitelist changed from 2|1 to 2` (Photon actor numbers). One event per person added or removed."""
        m = RE_WHITELIST.search(line)
        if not m:
            return []

        def nums(text: str) -> set[int]:
            """`2|1,6|1,5|1`: one entry per person, `<actor number>|<flag>`, separated by commas."""
            return {int(e.split("|")[0]) for e in text.split(",") if e.split("|")[0].strip().isdigit()}

        old, new = nums(m.group(1)), nums(m.group(2))
        out = []
        for actor, on in [(a, True) for a in sorted(new - old)] + [(a, False) for a in sorted(old - new)]:
            name = self.actors.get(actor, f"player {actor}")
            out.append(self._ev(ts, "vip_guest", name=name, usr=self.present.get(name, ""), on=on))
        return out

    def _role(self, ts: float, line: str) -> list[dict]:
        m = RE_ROLE.search(line)
        if not m:
            return []
        role, prev, nxt, name = m.group(1), m.group(2) == "True", m.group(3) == "True", m.group(4)
        if prev == nxt:
            return []
        usr = self.present.get(name, "")
        joined = self.joined_at.get(usr)
        state = self.roster or (joined is not None and ts - joined <= STATE_WINDOW and not prev)
        by = self.role_actor.get(name, "")
        if by == name:  # newer VRChat versions repeat the person in that argument: it is no giver
            by = ""
        if ts - self.toggle[0] <= TOGGLE_WINDOW and self.toggle[1]:  # a `_ToggleRole` line right before it: somebody really pressed the button
            by, state = self.toggle[1], False if not self.roster else state
        self.recent_roles[(name, role, nxt)] = ts
        if role == "Video" and nxt and not state:
            self.video_giver = (ts, name, usr)
        ev = self._ev(ts, "role", usr=usr, name=name, role=role, on=nxt, by=by, state=state)
        if not usr and name != self.local_name:  # not joined yet: hold it back until the join line gives the usr_ ID
            self.pending_roles.setdefault(name, []).append(ev)
            return []
        return [ev]


def parse_lines(lines, tz=None) -> list[dict]:
    """Convenience for tests / one-off analysis: all events of a list of lines."""
    p = LogParser(tz)
    out: list[dict] = []
    for line in lines:
        out.extend(p.feed(line))
    return out


def avatar_ids_from(data: bytes, limit: int = AVATAR_IDS_MAX) -> list[str]:
    """The avatar IDs (`avtr_<uuid>`) in a chunk of bytes (plain text and UTF-16LE), lower case, each once, in the order they stand in the data.
    ONLY the matched strings are returned: nothing around them (the file next to them holds personal data of the person who wears the log) is looked at or kept."""
    hits = [(m.start(), m.group(0).decode("ascii")) for m in _AVTR_FIND.finditer(data)]
    hits += [(m.start(), m.group(0).decode("utf-16-le")) for m in _AVTR_FIND16.finditer(data)]
    found: dict[str, None] = {}
    for _, text in sorted(hits):
        found.setdefault(text.lower(), None)
        if len(found) >= limit:
            break
    return list(found)
