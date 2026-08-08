"""Read local Cursor agent transcripts and terminal files.

Transcript lines are one of:
    {"role": "user"|"assistant", "message": {"content": [blocks]}}
    {"type": "turn_ended", "status": "success"|"error"|"aborted"}

Results are cached on (mtime, size) so the 1.5s UI poll stays cheap.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .utils import project_label, short_id, truncate

CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"

_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)

RUNNING = "running"
DONE = "done"
IDLE = "idle"

# A transcript with no turn_ended is only "running" if it was touched recently.
RUNNING_WINDOW_SECONDS = 15 * 60
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_LIMIT = 12


@dataclass(slots=True)
class TodoItem:
    content: str
    status: str  # pending | in_progress | completed | cancelled

    @property
    def glyph(self) -> str:
        if self.status == "completed":
            return "\u2713"
        if self.status == "in_progress":
            return "\u25ba"
        return "\u00b7"


@dataclass(slots=True)
class AgentInfo:
    agent_id: str
    project: str
    status: str
    task: str
    detail: str
    updated: float
    todos: list[TodoItem] = field(default_factory=list)
    device: str = ""
    source: str = "local"  # local | device | cloud
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["todos"] = [asdict(t) for t in self.todos]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentInfo":
        todos = [
            TodoItem(content=str(t.get("content", "")), status=str(t.get("status", "")))
            for t in data.get("todos", [])
            if isinstance(t, dict)
        ]
        return cls(
            agent_id=str(data.get("agent_id", "")),
            project=str(data.get("project", "")),
            status=str(data.get("status", IDLE)),
            task=str(data.get("task", "")),
            detail=str(data.get("detail", "")),
            updated=float(data.get("updated", 0.0) or 0.0),
            todos=todos,
            device=str(data.get("device", "")),
            source=str(data.get("source", "local")),
            url=str(data.get("url", "")),
        )


@dataclass(slots=True)
class ShellInfo:
    command: str
    cwd: str
    status: str
    updated: float
    device: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShellInfo":
        return cls(
            command=str(data.get("command", "")),
            cwd=str(data.get("cwd", "")),
            status=str(data.get("status", "")),
            updated=float(data.get("updated", 0.0) or 0.0),
            device=str(data.get("device", "")),
        )


# path -> (mtime, size, AgentInfo)
_TRANSCRIPT_CACHE: dict[str, tuple[float, int, AgentInfo]] = {}
_SHELL_CACHE: dict[str, tuple[float, int, ShellInfo]] = {}


def _iter_json_lines(path: str) -> Iterable[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _text_of(block: dict[str, Any]) -> str:
    value = block.get("text")
    return value if isinstance(value, str) else ""


def parse_transcript(path: str, mtime: float) -> AgentInfo:
    """Reduce one transcript file to a display record."""
    agent_id = Path(path).stem
    try:
        project_dir = Path(path).parents[2].name
    except IndexError:
        project_dir = "unknown"

    task = ""
    last_tools: list[str] = []
    todos: list[TodoItem] = []
    final_status: str | None = None
    final_error = ""
    saw_turn = False

    for obj in _iter_json_lines(path):
        if obj.get("type") == "turn_ended":
            final_status = str(obj.get("status", "success"))
            final_error = str(obj.get("error", "") or "")
            saw_turn = True
            continue

        role = obj.get("role")
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        if role == "user":
            joined = "\n".join(
                _text_of(b) for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
            matches = _USER_QUERY_RE.findall(joined)
            if matches:
                task = matches[-1].strip()
            elif joined.strip() and not task:
                task = joined.strip()
            # A new user turn supersedes the previous completion.
            final_status = None
            saw_turn = False
            last_tools = []
            continue

        if role == "assistant":
            tools: list[str] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", "") or "")
                if name:
                    tools.append(name)
                if name == "TodoWrite":
                    raw = block.get("input")
                    if isinstance(raw, dict) and isinstance(raw.get("todos"), list):
                        todos = [
                            TodoItem(
                                content=str(t.get("content", "")),
                                status=str(t.get("status", "pending")),
                            )
                            for t in raw["todos"]
                            if isinstance(t, dict)
                        ]
            if tools:
                last_tools = tools

    age = time.time() - mtime
    if saw_turn and final_status is not None:
        status = DONE
    elif age <= RUNNING_WINDOW_SECONDS:
        status = RUNNING
    else:
        status = IDLE

    detail = _build_detail(status, final_status, final_error, last_tools, todos)

    return AgentInfo(
        agent_id=agent_id,
        project=project_label(project_dir),
        status=status,
        task=summarize_task(task) or "(no prompt captured)",
        detail=detail,
        updated=mtime,
        todos=todos,
        source="local",
    )


def summarize_task(text: str, limit: int = 150) -> str:
    """First meaningful line(s) of a prompt, rather than one flattened blob.

    Prompts often open with a short title line, so keep pulling lines in
    until there is enough to read.
    """
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\]\)", " ", text or "")
    cleaned = re.sub(r"\[[^\]]*Image[^\]]*\]", " ", cleaned, flags=re.IGNORECASE)
    lines = [line.strip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line and not line.startswith("<")]
    if not lines:
        return truncate(cleaned, limit)

    summary = lines[0]
    index = 1
    while len(summary) < 40 and index < min(len(lines), 4):
        summary = f"{summary} {lines[index]}"
        index += 1
    return truncate(summary, limit)


def _build_detail(
    status: str,
    final_status: str | None,
    final_error: str,
    tools: list[str],
    todos: list[TodoItem],
) -> str:
    if status == DONE:
        label = final_status or "success"
        if final_error:
            return f"finished ({label}): {truncate(final_error, 60)}"
        return f"finished ({label})"

    parts: list[str] = []
    if tools:
        seen: list[str] = []
        for name in tools:
            if name not in seen:
                seen.append(name)
        parts.append(", ".join(seen[:3]))
    if todos:
        done = sum(1 for t in todos if t.status == "completed")
        parts.append(f"todos {done}/{len(todos)}")
    if not parts:
        return "waiting" if status == IDLE else "working"
    return " \u00b7 ".join(parts)


def local_agents(
    limit: int = DEFAULT_LIMIT,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> list[AgentInfo]:
    """Recent local agents, most recently active first."""
    if not CURSOR_PROJECTS.is_dir():
        return []

    pattern = str(CURSOR_PROJECTS / "*" / "agent-transcripts" / "*" / "*.jsonl")
    try:
        paths = glob.glob(pattern)
    except OSError:
        return []

    cutoff = time.time() - max_age_hours * 3600
    candidates: list[tuple[float, int, str]] = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime < cutoff or stat.st_size == 0:
            continue
        candidates.append((stat.st_mtime, stat.st_size, path))

    candidates.sort(reverse=True)
    agents: list[AgentInfo] = []
    for mtime, size, path in candidates[:limit]:
        cached = _TRANSCRIPT_CACHE.get(path)
        if cached and cached[0] == mtime and cached[1] == size:
            agents.append(cached[2])
            continue
        try:
            info = parse_transcript(path, mtime)
        except Exception:
            continue
        _TRANSCRIPT_CACHE[path] = (mtime, size, info)
        agents.append(info)

    _prune(_TRANSCRIPT_CACHE, {p for _, _, p in candidates})
    agents.sort(key=lambda a: (a.status != RUNNING, -a.updated))
    return agents


def _parse_terminal_header(path: str) -> dict[str, str]:
    """Read the leading `---` metadata block of a terminal file."""
    header: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
            if first != "---":
                return header
            for _ in range(24):
                line = handle.readline()
                if not line or line.strip() == "---":
                    break
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip('"')
                header[key.strip()] = value
    except OSError:
        return header
    return header


def local_shells(limit: int = 6, max_age_hours: float = 72.0) -> list[ShellInfo]:
    if not CURSOR_PROJECTS.is_dir():
        return []
    pattern = str(CURSOR_PROJECTS / "*" / "terminals" / "*.txt")
    try:
        paths = glob.glob(pattern)
    except OSError:
        return []

    cutoff = time.time() - max_age_hours * 3600
    candidates: list[tuple[float, int, str]] = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime < cutoff or stat.st_size == 0:
            continue
        candidates.append((stat.st_mtime, stat.st_size, path))

    candidates.sort(reverse=True)
    shells: list[ShellInfo] = []
    for mtime, size, path in candidates[:limit]:
        cached = _SHELL_CACHE.get(path)
        if cached and cached[0] == mtime and cached[1] == size:
            shells.append(cached[2])
            continue
        header = _parse_terminal_header(path)
        info = ShellInfo(
            command=truncate(header.get("command", "") or header.get("last_command", ""), 120),
            cwd=header.get("cwd", ""),
            status=header.get("status", "") or "done",
            updated=mtime,
        )
        if not info.command:
            continue
        _SHELL_CACHE[path] = (mtime, size, info)
        shells.append(info)

    _prune(_SHELL_CACHE, {p for _, _, p in candidates})
    return shells


def _prune(cache: dict[str, Any], keep: set[str]) -> None:
    if len(cache) <= 512:
        return
    for key in [k for k in cache if k not in keep]:
        cache.pop(key, None)


@dataclass(slots=True)
class DeviceGroup:
    name: str
    agents: list[AgentInfo]
    shells: list[ShellInfo]
    updated: float


@dataclass(slots=True)
class Snapshot:
    """Everything the right pane renders in one refresh."""

    local: list[AgentInfo] = field(default_factory=list)
    peers: list[DeviceGroup] = field(default_factory=list)
    cloud: list[AgentInfo] = field(default_factory=list)
    shells: list[ShellInfo] = field(default_factory=list)
    sync_path: str = ""
    cloud_note: str = ""

    @property
    def running_count(self) -> int:
        total = sum(1 for a in self.local if a.status == RUNNING)
        total += sum(
            1 for g in self.peers for a in g.agents if a.status == RUNNING
        )
        total += sum(1 for a in self.cloud if a.status == RUNNING)
        return total

    @property
    def done_count(self) -> int:
        total = sum(1 for a in self.local if a.status == DONE)
        total += sum(1 for g in self.peers for a in g.agents if a.status == DONE)
        total += sum(1 for a in self.cloud if a.status == DONE)
        return total


def build_snapshot(include_cloud: bool = True) -> Snapshot:
    """Merge local, peer-device and cloud activity. Never raises."""
    from . import cloud_agents, devices

    snapshot = Snapshot()

    try:
        snapshot.local = local_agents()
    except Exception:
        snapshot.local = []

    try:
        snapshot.shells = local_shells()
    except Exception:
        snapshot.shells = []

    try:
        snapshot.peers = devices.read_peers()
        snapshot.sync_path = str(devices.devices_dir())
    except Exception:
        snapshot.peers = []

    if include_cloud:
        try:
            snapshot.cloud, snapshot.cloud_note = cloud_agents.get_cloud_agents()
        except Exception:
            snapshot.cloud, snapshot.cloud_note = [], "unavailable"

    return snapshot


def agent_handle(agent: AgentInfo) -> str:
    """'project · a1b2c3d4' meta string."""
    bits = [agent.project or "unknown", short_id(agent.agent_id)]
    if agent.device:
        bits.insert(0, agent.device)
    return " \u00b7 ".join(b for b in bits if b)
