"""Cross-Mac sync through a shared folder.

Local IDE chats do not sync through a Cursor account, so each machine
publishes `<device>.json` into a shared (iCloud by default) directory and
reads its peers' files back.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from . import gist_sync
from .cursor_status import AgentInfo, DeviceGroup, ShellInfo, local_agents, local_shells

ICLOUD_DIR = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "yt-tui-devices"
)
FALLBACK_DIR = Path.home() / ".yt-tui" / "devices"

STALE_SECONDS = 15 * 60
PUBLISH_INTERVAL = 2.0
# Push immediately when agent state changes; otherwise heartbeat so peers
# do not look stale while nothing is happening.
PUBLISH_HEARTBEAT_SECONDS = 8.0

MACHINE_ID_PATH = Path.home() / ".yt-tui" / "machine-id"

_machine_id: str | None = None
_last_publish_fingerprint: str | None = None
_last_gist_publish = 0.0


def machine_id() -> str:
    """A stable id for this Mac, independent of its hostname.

    Filenames alone are not enough to recognise our own file: if the
    hostname changes, the old file would otherwise show up as a peer.
    """
    global _machine_id
    if _machine_id is not None:
        return _machine_id

    try:
        if MACHINE_ID_PATH.is_file():
            existing = MACHINE_ID_PATH.read_text(encoding="utf-8").strip()
            if existing:
                _machine_id = existing
                return existing
    except OSError:
        pass

    generated = uuid.uuid4().hex[:16]
    try:
        MACHINE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_PATH.write_text(generated, encoding="utf-8")
    except OSError:
        # Cannot persist, so derive something stable for this session
        # instead of a fresh random value on every call.
        seed = f"{socket.gethostname()}:{Path.home()}".encode("utf-8", "replace")
        generated = hashlib.md5(seed, usedforsecurity=False).hexdigest()[:16]

    _machine_id = generated
    return generated


def device_name() -> str:
    """Label for this machine, overridable with YT_TUI_DEVICE_NAME."""
    override = os.environ.get("YT_TUI_DEVICE_NAME", "").strip()
    if override:
        return _safe_name(override)
    try:
        host = socket.gethostname()
    except OSError:
        host = "mac"
    host = host.split(".")[0].removesuffix("-local").removesuffix("-Local")
    return _safe_name(host or "mac")


def _safe_name(value: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in value.strip())
    return cleaned.strip("-") or "mac"


def _same_device(a: str, b: str) -> bool:
    """Compare device labels ignoring case and hostname suffixes."""

    def normalize(value: str) -> str:
        text = _safe_name(str(value)).lower()
        for suffix in ("-local", "-lan", "-home"):
            text = text.removesuffix(suffix)
        return text

    return bool(a) and normalize(a) == normalize(b)


def devices_dir() -> Path:
    """Resolve the shared folder: env override, then iCloud, then local."""
    override = os.environ.get("YT_TUI_DEVICES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if ICLOUD_DIR.parent.is_dir():
        return ICLOUD_DIR
    return FALLBACK_DIR


def ensure_dir() -> Path | None:
    target = devices_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        if target != FALLBACK_DIR:
            try:
                FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
                return FALLBACK_DIR
            except OSError:
                return None
        return None


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    """Hash of the meaningful bits (ignore moving timestamps)."""
    agents = []
    for item in payload.get("agents") or []:
        if not isinstance(item, dict):
            continue
        agents.append(
            {
                "id": item.get("agent_id"),
                "status": item.get("status"),
                "task": item.get("task"),
                "detail": item.get("detail"),
                "todos": [
                    (t.get("content"), t.get("status"))
                    for t in (item.get("todos") or [])
                    if isinstance(t, dict)
                ],
            }
        )
    shells = []
    for item in payload.get("shells") or []:
        if not isinstance(item, dict):
            continue
        shells.append(
            {
                "command": item.get("command"),
                "status": item.get("status"),
            }
        )
    slim = {
        "device": payload.get("device"),
        "machine_id": payload.get("machine_id"),
        "agents": agents,
        "shells": shells,
    }
    return hashlib.md5(
        json.dumps(slim, sort_keys=True, ensure_ascii=False).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def publish(agents: list[AgentInfo] | None = None, shells: list[ShellInfo] | None = None) -> Path | None:
    """Write this machine's activity locally and to the shared GitHub gist."""
    global _last_publish_fingerprint, _last_gist_publish

    try:
        agents = local_agents() if agents is None else agents
        shells = local_shells() if shells is None else shells
    except Exception:
        agents, shells = agents or [], shells or []

    name = device_name()
    payload: dict[str, Any] = {
        "device": name,
        "machine_id": machine_id(),
        "updated": time.time(),
        "version": 2,
        "agents": [a.to_dict() for a in agents],
        "shells": [s.to_dict() for s in shells],
    }

    path: Path | None = None
    target = ensure_dir()
    if target is not None:
        path = target / f"{name}.json"
        _remove_stale_self(target, path)
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(target),
                prefix=".tmp-",
                suffix=".json",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                tmp_path = handle.name
            os.replace(tmp_path, path)
        except OSError:
            path = None

    fingerprint = _payload_fingerprint(payload)
    now = time.time()
    changed = fingerprint != _last_publish_fingerprint
    due = (now - _last_gist_publish) >= PUBLISH_HEARTBEAT_SECONDS
    gist_ok = False
    if changed or due:
        try:
            gist_ok = gist_sync.publish_to_gist(f"{name}.json", payload)
        except Exception:
            gist_ok = False
        if gist_ok:
            _last_publish_fingerprint = fingerprint
            _last_gist_publish = now

    if path is not None:
        return path
    if gist_ok or (_last_gist_publish and not changed and not due):
        return Path(f"gist:{name}.json")
    return None


def _group_from_payload(
    raw: dict[str, Any],
    fallback_name: str,
    stale_seconds: float = STALE_SECONDS,
) -> DeviceGroup | None:
    """Turn one device JSON payload into a DeviceGroup, or None if it is us/stale."""
    me = device_name()
    my_id = machine_id()
    now = time.time()

    name = str(raw.get("device", fallback_name)) or fallback_name
    if raw.get("machine_id") == my_id:
        return None
    if _same_device(name, me) or _same_device(fallback_name, me):
        return None

    updated = float(raw.get("updated", 0.0) or 0.0)
    if not updated or now - updated > stale_seconds:
        return None

    agents: list[AgentInfo] = []
    for item in raw.get("agents", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            info = AgentInfo.from_dict(item)
        except Exception:
            continue
        info.device = name
        info.source = "device"
        agents.append(info)

    shells: list[ShellInfo] = []
    for item in raw.get("shells", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            shell = ShellInfo.from_dict(item)
        except Exception:
            continue
        shell.device = name
        shells.append(shell)

    return DeviceGroup(name=name, agents=agents, shells=shells, updated=updated)


def _remove_stale_self(target: Path, current: Path) -> None:
    """Delete files this machine wrote under a previous name.

    Renaming a Mac (or a `.local` hostname sneaking in) would otherwise
    leave an orphan file that the machine then lists as its own peer.
    """
    me = machine_id()
    try:
        candidates = list(target.glob("*.json"))
    except OSError:
        return

    for path in candidates:
        if path == current or path.name.startswith("."):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        recorded = raw.get("machine_id")
        is_self = recorded == me or (
            recorded is None
            and (
                _same_device(str(raw.get("device", "")), device_name())
                or _same_device(path.stem, device_name())
            )
        )
        if is_self:
            try:
                path.unlink()
            except OSError:
                pass


def read_peers(stale_seconds: float = STALE_SECONDS) -> list[DeviceGroup]:
    """Load peer devices from the local folder and the shared GitHub gist."""
    by_name: dict[str, DeviceGroup] = {}

    def _add(group: DeviceGroup | None) -> None:
        if group is None:
            return
        existing = by_name.get(group.name)
        if existing is None or group.updated >= existing.updated:
            by_name[group.name] = group

    # Local / iCloud folder.
    target = devices_dir()
    if not target.is_dir() and FALLBACK_DIR.is_dir():
        target = FALLBACK_DIR
    if target.is_dir():
        try:
            files = sorted(target.glob("*.json"))
        except OSError:
            files = []
        for path in files:
            if path.name.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict):
                _add(_group_from_payload(raw, path.stem, stale_seconds))

    # GitHub gist (works even when iCloud does not sync).
    try:
        for filename, raw in gist_sync.read_gist_files().items():
            _add(_group_from_payload(raw, Path(filename).stem, stale_seconds))
    except Exception:
        pass

    groups = list(by_name.values())
    groups.sort(key=lambda g: -g.updated)
    return groups


def status_report() -> str:
    """Human-readable summary for `yt-tui --devices`."""
    target = devices_dir()
    lines = [
        f"device name : {device_name()}",
        f"machine id  : {machine_id()}",
        f"sync folder : {target}",
        f"exists      : {'yes' if target.is_dir() else 'no (run: yt-tui --devices init)'}",
        gist_sync.gist_status_line(),
    ]

    if target.is_dir():
        try:
            files = sorted(p.name for p in target.glob("*.json") if not p.name.startswith("."))
        except OSError:
            files = []
        lines.append(f"local files : {', '.join(files) if files else '(none)'}")

    try:
        gist_files = sorted(gist_sync.read_gist_files())
    except Exception:
        gist_files = []
    lines.append(f"gist files  : {', '.join(gist_files) if gist_files else '(none)'}")

    peers = read_peers()
    if peers:
        lines.append("")
        lines.append("peers:")
        now = time.time()
        for group in peers:
            running = sum(1 for a in group.agents if a.status == "running")
            age = int(now - group.updated)
            lines.append(
                f"  {group.name}: {len(group.agents)} agents "
                f"({running} running), {len(group.shells)} shells, {age}s ago"
            )
    else:
        lines.append("")
        lines.append("peers: none")
        lines.append("  tip: both Macs need `gh auth login` (same GitHub account),")
        lines.append("       then `yt-tui --devices init` and a running publisher.")

    return "\n".join(lines)
