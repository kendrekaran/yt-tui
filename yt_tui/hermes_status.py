"""Read Hermes gateway + cron state from ~/.hermes (no CLI spawn)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import humanize_age, truncate

HERMES_HOME = Path.home() / ".hermes"
JOBS_PATH = HERMES_HOME / "cron" / "jobs.json"
GATEWAY_PATH = HERMES_HOME / "gateway_state.json"
PROCESSES_PATH = HERMES_HOME / "processes.json"
TICK_LOCK = HERMES_HOME / "cron" / ".tick.lock"
CRON_OUTPUT = HERMES_HOME / "cron" / "output"


@dataclass(slots=True)
class HermesJob:
    job_id: str
    name: str
    enabled: bool
    state: str
    schedule: str
    next_run: float  # epoch
    last_run: float
    last_status: str
    last_error: str
    is_script: bool
    skill: str

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(slots=True)
class HermesSnapshot:
    available: bool = False
    gateway_running: bool = False
    gateway_pid: int = 0
    active_agents: int = 0
    platforms: list[str] = field(default_factory=list)
    jobs: list[HermesJob] = field(default_factory=list)
    ticking: bool = False
    note: str = ""

    @property
    def running_jobs(self) -> list[HermesJob]:
        return [j for j in self.jobs if j.running]

    @property
    def next_jobs(self) -> list[HermesJob]:
        now = time.time()
        upcoming = [
            j
            for j in self.jobs
            if j.enabled and j.state != "paused" and j.next_run > 0
        ]
        upcoming.sort(key=lambda j: j.next_run)
        # Prefer not-yet-due; if all overdue, still show them.
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
        state=str(raw.get("state") or ("scheduled" if raw.get("enabled", True) else "paused")),
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
        os_kill = __import__("os").kill
        os_kill(pid, 0)
        return True
    except OSError:
        return False


def _tick_in_progress() -> bool:
    """True while the scheduler is mid-tick (lock touched moments ago)."""
    try:
        if not TICK_LOCK.exists():
            return False
        return (time.time() - TICK_LOCK.stat().st_mtime) < 4
    except OSError:
        return False


def _infer_running_from_output(jobs: list[HermesJob]) -> None:
    """Mark a job running only if its output file is being written right now."""
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
            age = now - newest.stat().st_mtime
            # Only the open write window — completed minute jobs settle fast.
            if age < 1.5:
                job.state = "running"
        except (OSError, ValueError):
            continue


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

    if snap.active_agents > 0:
        snap.note = f"{snap.active_agents} agent(s) active"
    elif not snap.gateway_running:
        snap.note = "gateway off — cron won't fire"
    elif not snap.jobs:
        snap.note = "no cron jobs"

    return snap


def when_label(epoch: float, now: float | None = None) -> str:
    """Relative label for a future or past cron time."""
    if not epoch:
        return "—"
    current = now if now is not None else time.time()
    delta = epoch - current
    if abs(delta) < 10:
        return "now"
    if delta > 0:
        # future
        if delta < 60:
            return f"in {int(delta)}s"
        if delta < 3600:
            return f"in {int(delta // 60)}m"
        if delta < 86400:
            return f"in {int(delta // 3600)}h"
        return f"in {int(delta // 86400)}d"
    return humanize_age(epoch, current)
