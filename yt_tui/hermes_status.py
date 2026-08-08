"""Read Hermes gateway, cron, and live chat activity from ~/.hermes."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import humanize_age, truncate

HERMES_HOME = Path.home() / ".hermes"
JOBS_PATH = HERMES_HOME / "cron" / "jobs.json"
GATEWAY_PATH = HERMES_HOME / "gateway_state.json"
TICK_LOCK = HERMES_HOME / "cron" / ".tick.lock"
CRON_OUTPUT = HERMES_HOME / "cron" / "output"
SESSIONS_INDEX = HERMES_HOME / "sessions" / "sessions.json"
STATE_DB = HERMES_HOME / "state.db"
AGENT_LOG = HERMES_HOME / "logs" / "agent.log"
GATEWAY_LOG = HERMES_HOME / "logs" / "gateway.log"

# Chats with no update for this long are treated as idle (not live).
LIVE_WINDOW_SECONDS = 120.0
RECENT_WINDOW_SECONDS = 6 * 3600.0


@dataclass(slots=True)
class HermesJob:
    job_id: str
    name: str
    enabled: bool
    state: str
    schedule: str
    next_run: float
    last_run: float
    last_status: str
    last_error: str
    is_script: bool
    skill: str

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(slots=True)
class HermesChat:
    """A live or recent Hermes conversation (Telegram / WhatsApp / CLI / …)."""

    session_id: str
    platform: str
    user: str
    title: str
    task: str
    detail: str
    status: str  # running | done | idle
    updated: float
    tools: list[str] = field(default_factory=list)
    api_calls: int = 0


@dataclass(slots=True)
class HermesSnapshot:
    available: bool = False
    gateway_running: bool = False
    gateway_pid: int = 0
    active_agents: int = 0
    platforms: list[str] = field(default_factory=list)
    jobs: list[HermesJob] = field(default_factory=list)
    chats: list[HermesChat] = field(default_factory=list)
    ticking: bool = False
    note: str = ""

    @property
    def running_jobs(self) -> list[HermesJob]:
        return [j for j in self.jobs if j.running]

    @property
    def running_chats(self) -> list[HermesChat]:
        return [c for c in self.chats if c.status == "running"]

    @property
    def next_jobs(self) -> list[HermesJob]:
        now = time.time()
        upcoming = [
            j
            for j in self.jobs
            if j.enabled and j.state != "paused" and j.next_run > 0
        ]
        upcoming.sort(key=lambda j: j.next_run)
        future = [j for j in upcoming if j.next_run >= now - 5]
        return future or upcoming


def _parse_iso(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _job_from_dict(raw: dict[str, Any]) -> HermesJob | None:
    job_id = str(raw.get("id") or "")
    if not job_id:
        return None
    schedule = (
        str(raw.get("schedule_display") or "")
        or str((raw.get("schedule") or {}).get("display") or "")
        or str((raw.get("schedule") or {}).get("expr") or "")
    )
    script = raw.get("script")
    skill = str(raw.get("skill") or "")
    if not skill and isinstance(raw.get("skills"), list) and raw["skills"]:
        skill = str(raw["skills"][0])
    return HermesJob(
        job_id=job_id,
        name=str(raw.get("name") or job_id),
        enabled=bool(raw.get("enabled", True)),
        state=str(
            raw.get("state")
            or ("scheduled" if raw.get("enabled", True) else "paused")
        ),
        schedule=schedule,
        next_run=_parse_iso(raw.get("next_run_at")),
        last_run=_parse_iso(raw.get("last_run_at")),
        last_status=str(raw.get("last_status") or ""),
        last_error=str(raw.get("last_error") or raw.get("last_delivery_error") or ""),
        is_script=bool(script) or bool(raw.get("no_agent")),
        skill=skill or (str(script) if script else ""),
    )


def _gateway_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        __import__("os").kill(pid, 0)
        return True
    except OSError:
        return False


def _tick_in_progress() -> bool:
    try:
        if not TICK_LOCK.exists():
            return False
        return (time.time() - TICK_LOCK.stat().st_mtime) < 4
    except OSError:
        return False


def _infer_running_from_output(jobs: list[HermesJob]) -> None:
    if not CRON_OUTPUT.is_dir():
        return
    now = time.time()
    for job in jobs:
        if job.state in {"paused", "running"}:
            continue
        out_dir = CRON_OUTPUT / job.job_id
        if not out_dir.is_dir():
            continue
        try:
            newest = max(
                (p for p in out_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            if now - newest.stat().st_mtime < 1.5:
                job.state = "running"
        except (OSError, ValueError):
            continue


def _tail_text(path: Path, max_bytes: int = 120_000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


_INBOUND_RE = re.compile(
    r"inbound message: platform=(\w+) user=(.*?) chat=(\S+) "
    r"msg=(?:'(?P<msg_sq>(?:\\'|[^'])*)'|\"(?P<msg_dq>(?:\\\"|[^\"])*)\"|(?P<msg_bare>.+))\s*$"
)
_TURN_RE = re.compile(
    r"\[([^\]]+)\].*conversation turn: session=\1.*?platform=(\w+).*?msg=(['\"])(.*?)\3"
)
_TURN_RE2 = re.compile(
    r"\[(?P<sid>[^\]]+)\].*conversation turn:.*?platform=(?P<plat>\w+).*?msg=(?P<q>['\"])(?P<msg>.*?)(?P=q)"
)
_API_RE = re.compile(
    r"\[(?P<sid>[^\]]+)\].*API call #(?P<n>\d+): model=(?P<model>\S+)"
)
_TOOL_RE = re.compile(
    r"\[(?P<sid>[^\]]+)\].*tool(?:_executor: tool)? (?P<name>\S+) completed"
)
_TOOL_RE2 = re.compile(
    r"\[(?P<sid>[^\]]+)\] agent\.tool_executor: tool (?P<name>\S+) completed"
)
_ENDED_RE = re.compile(
    r"\[(?P<sid>[^\]]+)\].*Turn ended:.*?session=(?P=sid)|"
    r"response ready:.*?time="
)
_ENDED_SID_RE = re.compile(
    r"(?:Turn ended:.*?session=(?P<sid>\S+)|\[(?P<sid2>[^\]]+)\].*Turn ended:)"
)


def _live_activity_from_logs() -> dict[str, dict[str, Any]]:
    """session_id -> {status, detail, task, platform, tools, updated} from recent logs."""
    now = time.time()
    text = _tail_text(AGENT_LOG) + "\n" + _tail_text(GATEWAY_LOG, 80_000)
    activity: dict[str, dict[str, Any]] = {}

    for line in text.splitlines():
        # Timestamp at start: 2026-08-08 06:28:30
        ts = 0.0
        if len(line) >= 19 and line[4] == "-" and line[10] == " ":
            try:
                ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                ts = 0.0
        if ts and now - ts > RECENT_WINDOW_SECONDS:
            continue

        m = _INBOUND_RE.search(line)
        if m:
            # inbound has no session id yet — stash under platform:chat for merge
            key = f"inbound:{m.group(1)}:{m.group(3)}"
            task = m.group("msg_sq") or m.group("msg_dq") or m.group("msg_bare") or ""
            activity[key] = {
                "status": "running",
                "platform": m.group(1),
                "user": m.group(2),
                "task": task,
                "detail": "received",
                "tools": [],
                "updated": ts or now,
            }
            continue

        m = _TURN_RE2.search(line)
        if m:
            sid = m.group("sid")
            rec = activity.setdefault(sid, {"tools": [], "status": "running"})
            rec.update(
                {
                    "status": "running",
                    "platform": m.group("plat"),
                    "task": m.group("msg"),
                    "detail": "thinking",
                    "updated": ts or now,
                }
            )
            continue

        m = _API_RE.search(line)
        if m:
            sid = m.group("sid")
            rec = activity.setdefault(sid, {"tools": [], "status": "running"})
            rec["status"] = "running"
            rec["detail"] = f"api #{m.group('n')} · {m.group('model')}"
            rec["updated"] = ts or now
            continue

        m = _TOOL_RE2.search(line) or _TOOL_RE.search(line)
        if m:
            sid = m.group("sid")
            name = m.group("name")
            rec = activity.setdefault(sid, {"tools": [], "status": "running"})
            tools = list(rec.get("tools") or [])
            if name not in tools:
                tools.append(name)
            rec["tools"] = tools[-4:]
            rec["status"] = "running"
            rec["detail"] = f"tool {name}"
            rec["updated"] = ts or now
            continue

        m = _ENDED_SID_RE.search(line)
        if m:
            sid = m.group("sid") or m.group("sid2")
            if sid:
                rec = activity.setdefault(sid, {"tools": []})
                rec["status"] = "done"
                rec["detail"] = "finished"
                rec["updated"] = ts or now
            continue

        if "response ready:" in line:
            # Mark most recent running telegram/inbound as done if we can.
            for key, rec in list(activity.items()):
                if rec.get("status") == "running" and rec.get("platform") in {
                    "telegram",
                    "whatsapp",
                    "discord",
                    "slack",
                }:
                    # Only close if this response line is newer.
                    if ts >= float(rec.get("updated") or 0):
                        rec["status"] = "done"
                        rec["detail"] = "replied"
                        rec["updated"] = ts or now

    return activity


def _session_messages(session_id: str) -> tuple[str, str, list[str]]:
    """Return (last_user_text, last_assistant_preview, recent_tools)."""
    if not STATE_DB.is_file():
        return "", "", []
    try:
        connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return "", "", []

    task = ""
    reply = ""
    tools: list[str] = []
    try:
        rows = connection.execute(
            "SELECT role, content, tool_name, tool_calls FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT 30",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()

    for role, content, tool_name, tool_calls in rows:
        if role == "user" and not task and content:
            task = str(content).strip()
        elif role == "assistant" and not reply and content:
            reply = str(content).strip()
        elif role == "tool" and tool_name:
            name = str(tool_name)
            if name not in tools:
                tools.append(name)
        elif role == "assistant" and tool_calls:
            try:
                calls = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
            except (ValueError, TypeError):
                calls = []
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        name = str(
                            call.get("name")
                            or (call.get("function") or {}).get("name")
                            or ""
                        )
                        if name and name not in tools:
                            tools.append(name)
    tools.reverse()
    return task, reply, tools[-4:]


def _db_session(session_id: str) -> dict[str, Any] | None:
    if not STATE_DB.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, source, title, started_at, ended_at, message_count, "
            "tool_call_count, api_call_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        connection.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def _best_task(task: str, title: str) -> str:
    """Prefer a descriptive title when the last user msg is a short ping."""
    task = (task or "").strip()
    title = (title or "").strip()
    if not task:
        return title
    if title and len(task) <= 8 and len(title) > len(task) + 4:
        return title
    return task


def _epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_iso(value)


def _recent_db_sessions() -> list[dict[str, Any]]:
    """Recent non-cron sessions from state.db (Telegram / CLI / …)."""
    if not STATE_DB.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        cutoff = time.time() - RECENT_WINDOW_SECONDS
        rows = connection.execute(
            "SELECT id, source, title, started_at, ended_at, message_count, "
            "tool_call_count, api_call_count FROM sessions "
            "WHERE started_at >= ? AND IFNULL(source, '') != 'cron' "
            "ORDER BY started_at DESC LIMIT 20",
            (cutoff,),
        ).fetchall()
        connection.close()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _build_chat(
    *,
    sid: str,
    platform: str,
    user: str,
    title: str,
    db: dict[str, Any],
    live: dict[str, Any],
    updated: float,
    now: float,
) -> HermesChat | None:
    task, reply, tools = _session_messages(sid)
    status = "idle"
    detail = ""

    if live:
        status = str(live.get("status") or "idle")
        detail = str(live.get("detail") or "")
        if live.get("task"):
            task = str(live["task"])
        if live.get("tools"):
            tools = list(dict.fromkeys([*tools, *live["tools"]]))[-4:]
        if live.get("updated"):
            updated = max(updated, float(live["updated"]))
        if live.get("platform"):
            platform = str(live["platform"])
    elif db.get("ended_at"):
        status = "done"
        detail = "finished"
    elif updated and now - updated <= LIVE_WINDOW_SECONDS:
        if not reply:
            status = "running"
            detail = "working"
        else:
            status = "done"
            detail = truncate(reply.replace("\n", " "), 60)
    elif reply:
        status = "done"
        detail = truncate(reply.replace("\n", " "), 60)

    task = _best_task(task, title)
    if not task:
        task = title or sid[:12]

    age = now - updated if updated else 9999.0
    if status == "idle":
        if age > 30 * 60:
            return None
        status = "done"
        detail = detail or "finished"
    elif status == "done" and age > 30 * 60:
        return None
    elif status == "running" and age > LIVE_WINDOW_SECONDS:
        # Stale "running" from an incomplete log parse — demote.
        status = "done"
        detail = detail or "finished"

    return HermesChat(
        session_id=sid,
        platform=platform or "chat",
        user=user,
        title=truncate(title or task, 80),
        task=truncate(task.replace("\n", " "), 120),
        detail=truncate(detail, 80),
        status=status,
        updated=updated or _epoch(db.get("started_at")) or now,
        tools=tools,
        api_calls=int(db.get("api_call_count") or 0),
    )


def _collect_chats() -> list[HermesChat]:
    now = time.time()
    log_activity = _live_activity_from_logs()
    chats: dict[str, HermesChat] = {}

    # 1) Active channel sessions from sessions.json
    index = _load_json(SESSIONS_INDEX)
    if isinstance(index, dict):
        for _key, meta in index.items():
            if not isinstance(meta, dict):
                continue
            if meta.get("expiry_finalized"):
                continue
            sid = str(meta.get("session_id") or "")
            if not sid:
                continue
            updated = _parse_iso(meta.get("updated_at"))
            if updated and now - updated > RECENT_WINDOW_SECONDS:
                continue

            db = _db_session(sid) or {}
            title = str(db.get("title") or meta.get("display_name") or sid[:12])
            platform = str(meta.get("platform") or db.get("source") or "chat")
            user = str(
                (meta.get("origin") or {}).get("user_name")
                or meta.get("display_name")
                or ""
            )
            chat = _build_chat(
                sid=sid,
                platform=platform,
                user=user,
                title=title,
                db=db,
                live=log_activity.get(sid, {}),
                updated=updated or _epoch(db.get("started_at")),
                now=now,
            )
            if chat:
                chats[sid] = chat

    # 2) Recent sessions from state.db (CLI + anything missing from the index)
    for db in _recent_db_sessions():
        sid = str(db.get("id") or "")
        if not sid or sid in chats:
            continue
        chat = _build_chat(
            sid=sid,
            platform=str(db.get("source") or "chat"),
            user="",
            title=str(db.get("title") or ""),
            db=db,
            live=log_activity.get(sid, {}),
            updated=_epoch(db.get("ended_at") or db.get("started_at")),
            now=now,
        )
        if chat:
            # Prefer log mtime when newer (mid-turn updates).
            live = log_activity.get(sid, {})
            if live.get("updated"):
                chat.updated = max(chat.updated, float(live["updated"]))
            chats[sid] = chat

    # 3) Log-only sessions (mid-turn before DB/index catches up, plus fresh done)
    for key, live in log_activity.items():
        if key.startswith("inbound:"):
            # Merge into an existing platform session when possible; otherwise show
            # the inbound row so Telegram appears the moment the message lands.
            _, plat, _chat_id = key.split(":", 2)
            age = now - float(live.get("updated") or 0)
            merged = False
            for chat in chats.values():
                if chat.platform != plat:
                    continue
                # Same DM: refresh task/status from the fresher inbound line.
                if age <= LIVE_WINDOW_SECONDS and float(live.get("updated") or 0) >= chat.updated - 1:
                    if live.get("status") == "running" or chat.status == "running":
                        chat.status = str(live.get("status") or chat.status)
                        chat.task = truncate(str(live.get("task") or chat.task), 120)
                        chat.detail = str(live.get("detail") or chat.detail)
                        chat.updated = max(chat.updated, float(live.get("updated") or 0))
                        if live.get("user"):
                            chat.user = str(live["user"])
                        merged = True
                        break
            if merged:
                continue
            if live.get("status") == "running" and age <= LIVE_WINDOW_SECONDS:
                chats[key] = HermesChat(
                    session_id=key,
                    platform=str(live.get("platform") or "hermes"),
                    user=str(live.get("user") or ""),
                    title=truncate(str(live.get("task") or "message"), 80),
                    task=truncate(str(live.get("task") or "message"), 120),
                    detail=str(live.get("detail") or "received"),
                    status="running",
                    updated=float(live.get("updated") or now),
                    tools=[],
                )
            continue
        if key in chats:
            continue
        age = now - float(live.get("updated") or 0)
        if age > 30 * 60:
            continue
        status = str(live.get("status") or "done")
        if status == "running" and age > LIVE_WINDOW_SECONDS:
            status = "done"
        chats[key] = HermesChat(
            session_id=key,
            platform=str(live.get("platform") or "hermes"),
            user=str(live.get("user") or ""),
            title=truncate(str(live.get("task") or key), 80),
            task=truncate(str(live.get("task") or "working"), 120),
            detail=str(live.get("detail") or ("working" if status == "running" else "finished")),
            status=status,
            updated=float(live.get("updated") or now),
            tools=list(live.get("tools") or []),
        )

    result = list(chats.values())
    result.sort(key=lambda c: (c.status != "running", -c.updated))
    return result[:8]


def get_hermes_snapshot() -> HermesSnapshot:
    """Best-effort Hermes status. Never raises."""
    if not HERMES_HOME.is_dir():
        return HermesSnapshot(note="not installed")

    snap = HermesSnapshot(available=True)

    gw = _load_json(GATEWAY_PATH)
    if isinstance(gw, dict):
        snap.gateway_pid = int(gw.get("pid") or 0)
        snap.active_agents = int(gw.get("active_agents") or 0)
        snap.gateway_running = (
            str(gw.get("gateway_state") or "") == "running"
            and _gateway_alive(snap.gateway_pid)
        )
        platforms = gw.get("platforms") or {}
        if isinstance(platforms, dict):
            snap.platforms = [
                name
                for name, meta in platforms.items()
                if isinstance(meta, dict) and meta.get("state") == "connected"
            ]

    jobs_raw = _load_json(JOBS_PATH)
    if isinstance(jobs_raw, dict) and isinstance(jobs_raw.get("jobs"), list):
        for item in jobs_raw["jobs"]:
            if isinstance(item, dict):
                job = _job_from_dict(item)
                if job:
                    snap.jobs.append(job)

    _infer_running_from_output(snap.jobs)
    snap.ticking = _tick_in_progress() or any(j.running for j in snap.jobs)

    try:
        snap.chats = _collect_chats()
    except Exception:
        snap.chats = []

    running_chats = len(snap.running_chats)
    if running_chats:
        snap.note = f"{running_chats} chat(s) live"
    elif snap.active_agents > 0:
        snap.note = f"{snap.active_agents} agent(s) active"
    elif not snap.gateway_running:
        snap.note = "gateway off — cron won't fire"
    elif not snap.jobs and not snap.chats:
        snap.note = "idle"

    return snap


def when_label(epoch: float, now: float | None = None) -> str:
    if not epoch:
        return "—"
    current = now if now is not None else time.time()
    delta = epoch - current
    if abs(delta) < 10:
        return "now"
    if delta > 0:
        if delta < 60:
            return f"in {int(delta)}s"
        if delta < 3600:
            return f"in {int(delta // 60)}m"
        if delta < 86400:
            return f"in {int(delta // 3600)}h"
        return f"in {int(delta // 86400)}d"
    return humanize_age(epoch, current)
