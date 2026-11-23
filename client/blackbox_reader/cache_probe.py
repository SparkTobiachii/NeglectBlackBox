"""Neglect Black Box - cache probe (read only, nothing is sent anywhere).

Answers one question: can the avatar files VRChat keeps on this PC be read, so that the reader could take real numbers (triangles, materials, meshes) from them?
It lists the newest entries of the VRChat cache, shows what the info file says, checks whether the data file starts like an open Unity bundle ("UnityFS"),
and - only if you installed UnityPy (`pip install UnityPy`) - counts meshes / triangles / materials of a few of them.

Run:   python cache_probe.py            (writes cache_probe_report.txt next to the script, ~1 KB per entry; check it before you send it)
Cache: C:/VRChatCache/Cache-WindowsPlayer (the log says where: "Using custom cache directory"), else %LOCALAPPDATA%Low/VRChat/VRChat/Cache-WindowsPlayer"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

LIMIT = 12


def cache_dirs() -> list[Path]:
    found = [Path("C:/VRChatCache/Cache-WindowsPlayer")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local).parent / "LocalLow" / "VRChat" / "VRChat" / "Cache-WindowsPlayer")
    log_dir = Path(local).parent / "LocalLow" / "VRChat" / "VRChat" if local else None
    if log_dir and log_dir.exists():
        for log in sorted(log_dir.glob("output_log_*.txt"))[-1:]:
            for line in log.read_text("utf-8", errors="replace").splitlines()[:400]:
                m = re.search(r"Using custom cache directory: (.+)$", line)
                if m:
                    found.insert(0, Path(m.group(1).strip()))
    return [p for p in found if p.exists()]


def printable(raw: bytes, n: int = 300) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw[:n])


def probe(folder: Path, out: list[str]) -> None:
    entries = [p for p in folder.iterdir() if p.is_dir()]
    out.append(f"cache folder: {folder}  ({len(entries)} entries)")
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    try:
        import UnityPy  # type: ignore[import-not-found]
    except ImportError:
        UnityPy = None
        out.append("UnityPy is not installed: only the file headers are checked (pip install UnityPy for the numbers).")
    for entry in entries[:LIMIT]:
        versions = [p for p in entry.iterdir() if p.is_dir()] or [entry]
        for v in versions[:1]:
            data, info = v / "__data", v / "__info"
            out.append(f"- {entry.name[:16]}…/{v.name[:16] if v != entry else ''}")
            if info.exists():
                raw_info = info.read_bytes()
                out.append("    __info: " + printable(raw_info, 240))
                ids = sorted(set(re.findall(rb"(?:avtr|file|wrld)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw_info)))
                out.append("    ids in __info: " + (", ".join(i.decode() for i in ids[:4]) if ids else "none"))
            if not data.exists():
                out.append("    no __data file")
                continue
            head = data.read_bytes()[:16] if data.stat().st_size else b""
            if data.stat().st_size:
                with open(data, "rb") as fh:
                    blob = fh.read(2 * 1024 * 1024)
                ids = sorted(set(re.findall(rb"(?:avtr|file)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", blob)))
                out.append("    ids in the first 2 MB of __data: " + (", ".join(i.decode() for i in ids[:4]) if ids else "none"))
            out.append(f"    __data: {data.stat().st_size // 1024} KB, starts with {head[:8]!r} -> {'open Unity bundle' if head.startswith(b'UnityFS') else 'NOT a plain Unity bundle (encrypted or another format)'}")
            if UnityPy and head.startswith(b"UnityFS"):
                try:
                    env = UnityPy.load(str(data))
                    kinds: dict[str, int] = {}
                    tris = 0
                    for obj in env.objects:
                        kinds[obj.type.name] = kinds.get(obj.type.name, 0) + 1
                        if obj.type.name == "Mesh":
                            try:
                                mesh = obj.read()
                                tris += len(getattr(mesh, "m_IndexBuffer", b"")) // 6
                            except Exception:  # noqa: S110
                                pass
                    out.append(f"    readable: {kinds.get('Mesh', 0)} meshes, ~{tris} triangles, {kinds.get('Material', 0)} materials, {kinds.get('SkinnedMeshRenderer', 0)} skinned renderers")
                except Exception as e:
                    out.append(f"    UnityPy could not open it: {type(e).__name__}: {str(e)[:100]}")


def main() -> int:
    out: list[str] = ["Neglect Black Box cache probe"]
    dirs = cache_dirs()
    if not dirs:
        out.append("No VRChat cache folder found. Start VRChat once and look at the line 'Using custom cache directory' in its log.")
    for d in dirs[:1]:
        probe(d, out)
    report = Path(__file__).with_name("cache_probe_report.txt")
    report.write_text("\n".join(out), "utf-8")
    print("\n".join(out))
    print(f"\nReport written to {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
