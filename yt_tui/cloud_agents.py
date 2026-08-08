"""Cursor Background / Cloud agents.

Preferred source is the REST API (needs CURSOR_API_KEY). Without a key we
fall back to Cursor's local SQLite state cache. Both paths are best-effort:
any failure degrades to an empty list plus a short note.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .cursor_status import DONE, IDLE, RUNNING, AgentInfo
from .utils import truncate

API_URL = "https://api.cursor.com/v1/agents"
CACHE_SECONDS = 20.0
COMPLETED_WINDOW = 24 * 3600
REQUEST_TIMEOUT = 6.0

STATE_DB_CANDIDATES = (
    Path.home()
    / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
    Path.home()
    / "Library/Application Support/Cursor Nightly/User/globalStorage/state.vscdb",
    Path.home() / ".config/Cursor/User/globalStorage/state.vscdb",
)

_RUNNING_STATES = {"RUNNING", "PENDING", "CREATING", "ACTIVE", "QUEUED"}
_DONE_STATES = {"FINISHED", "COMPLETED", "SUCCEEDED", "DONE"}
_FAILED_STATES = {"ERROR", "FAILED", "EXPIRED", "CANCELLED", "CANCELED"}

# (timestamp, agents, note)
_cache: tuple[float, list[AgentInfo], str] = (0.0, [], "")


def get_cloud_agents(force: bool = False) -> tuple[list[AgentInfo], str]:
    """Cached cloud agent list. Returns (agents, note)."""
    global _cache
    now = time.time()
    if not force and now - _cache[0] < CACHE_SECONDS:
        return _cache[1], _cache[2]

    agents: list[AgentInfo] = []
    note = ""
    key = os.environ.get("CURSOR_API_KEY", "").strip()

    if key:
        try:
            agents = _fetch_from_api(key)
            note = "api"
        except Exception as exc:
            note = f"api: {_describe_error(exc)}"
            agents = []

    if not agents:
        try:
            cached = _read_state_cache()
        except Exception:
            cached = []
        if cached:
            agents = cached
            note = note or "local cache"
        elif not note:
            note = "no key / no cache"

    agents = _filter_recent(agents)
    _cache = (now, agents, note)
    return agents, note


def _describe_error(exc: Exception) -> str:
    """Short, non-scary reason to show in the sidebar footer."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401 or status == 403:
        return "key rejected"
    if status:
        return f"http {status}"
    if "Timeout" in type(exc).__name__:
        return "timeout"
    if "Connect" in type(exc).__name__:
        return "offline"
    return "unavailable"


def _filter_recent(agents: list[AgentInfo]) -> list[AgentInfo]:
    """Running agents, plus anything finished within the last day."""
    now = time.time()
    keep = [
        a
        for a in agents
        if a.status == RUNNING or (now - a.updated) <= COMPLETED_WINDOW
    ]
    keep.sort(key=lambda a: (a.status != RUNNING, -a.updated))
    return keep[:10]


def _fetch_from_api(key: str) -> list[AgentInfo]:
    import httpx

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(API_URL, auth=(key, ""), params={"limit": 25})
        response.raise_for_status()
        payload = response.json()

    raw = payload.get("agents") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return [info for info in (_agent_from_api(item) for item in raw) if info]


def _agent_from_api(item: Any) -> AgentInfo | None:
    if not isinstance(item, dict):
        return None

    agent_id = str(item.get("id", "") or "")
    if not agent_id:
        return None

    raw_status = str(item.get("status", "") or "").upper()
    status = _map_status(raw_status)

    updated = _parse_time(
        item.get("updatedAt") or item.get("createdAt") or item.get("created_at")
    )

    source = item.get("source")
    repo = ""
    if isinstance(source, dict):
        repo = str(source.get("repository", "") or "")
        repo = repo.rstrip("/").split("/")[-1].removesuffix(".git")

    target = item.get("target")
    url = ""
    if isinstance(target, dict):
        url = str(target.get("url", "") or "")

    task = str(item.get("name", "") or item.get("prompt", "") or "cloud agent")
    summary = str(item.get("summary", "") or "")
    detail = summary or raw_status.lower() or "cloud"

    return AgentInfo(
        agent_id=agent_id.removeprefix("bc-"),
        project=repo or "cloud",
        status=status,
        task=truncate(task, 160),
        detail=truncate(detail, 90),
        updated=updated or time.time(),
        source="cloud",
        url=url,
    )


def _map_status(raw: str) -> str:
    if raw in _RUNNING_STATES:
        return RUNNING
    if raw in _DONE_STATES or raw in _FAILED_STATES:
        return DONE
    return IDLE


def _parse_time(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        # Milliseconds vs seconds.
        return float(value) / 1000.0 if float(value) > 1e11 else float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _state_db() -> Path | None:
    for path in STATE_DB_CANDIDATES:
        if path.is_file():
            return path
    return None


def _read_state_cache() -> list[AgentInfo]:
    """Best-effort read of Cursor's cloud agent rows from state.vscdb."""
    path = _state_db()
    if path is None:
        return []

    uri = f"file:{path}?mode=ro&immutable=1"
    rows: list[tuple[str, Any]] = []
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return []

    try:
        cursor = connection.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'cloudAgentRepository.agents%'"
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()

    agents: list[AgentInfo] = []
    seen: set[str] = set()
    for _key, value in rows:
        for item in _walk_agent_dicts(_loads(value)):
            info = _agent_from_api(item)
            if info and info.agent_id not in seen:
                seen.add(info.agent_id)
                info.detail = info.detail or "from local cache"
                agents.append(info)
    return agents


def _loads(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _walk_agent_dicts(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find agent-shaped dicts anywhere in an unknown cache payload."""
    if depth > 6:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "id" in node and ("status" in node or "name" in node):
            found.append(node)
        else:
            for child in node.values():
                found.extend(_walk_agent_dicts(child, depth + 1))
    elif isinstance(node, list):
        for child in node:
            found.extend(_walk_agent_dicts(child, depth + 1))
    return found
