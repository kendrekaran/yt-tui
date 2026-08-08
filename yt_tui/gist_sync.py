"""Peer sync via a cloned private GitHub gist (git pull/push).

The REST gist PATCH endpoint rate-limits aggressively when polled every few
seconds. Cloning the gist and pushing with git stays reliable and fast.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

REPO_GIST_FILE = Path(__file__).resolve().parent.parent / "device-sync.gist"
LOCAL_GIST_FILE = Path.home() / ".yt-tui" / "gist-id"
GIST_REPO_DIR = Path.home() / ".yt-tui" / "gist-repo"
LOCK_PATH = Path.home() / ".yt-tui" / "gist-sync.lock"

_token: str | None = None
_token_checked = False
_last_pull = 0.0
_PULL_MIN_SECONDS = 1.5
_last_error: str = ""


def gist_id() -> str | None:
    override = os.environ.get("YT_TUI_GIST_ID", "").strip()
    if override:
        return override
    for path in (LOCAL_GIST_FILE, REPO_GIST_FILE):
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value and not value.startswith("#"):
                    return value.split()[0]
        except OSError:
            continue
    return None


def save_gist_id(value: str) -> None:
    try:
        LOCAL_GIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_GIST_FILE.write_text(value.strip() + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        REPO_GIST_FILE.write_text(value.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def _gh_bin() -> str:
    return shutil.which("gh") or "/usr/local/bin/gh"


def github_token() -> str | None:
    global _token, _token_checked
    if _token_checked:
        return _token
    _token_checked = True
    for key in ("YT_TUI_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            _token = value
            return _token
    try:
        result = subprocess.run(
            [_gh_bin(), "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        _token = None
        return None
    if result.returncode == 0 and result.stdout.strip():
        _token = result.stdout.strip()
        return _token
    _token = None
    return None


def last_error() -> str:
    return _last_error


def _set_error(message: str) -> None:
    global _last_error
    _last_error = message


def _run(args: list[str], *, cwd: Path | None = None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _remote_url(gist: str) -> str | None:
    token = github_token()
    if not token:
        return None
    return f"https://x-access-token:{token}@gist.github.com/{gist}.git"


def _with_lock(timeout: float = 8.0) -> bool:
    """Naive exclusive lock so TUI + LaunchAgent do not push at once."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
                if age > 30:
                    LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.15)
        except OSError:
            return False
    return False


def _unlock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_gist() -> str | None:
    """Return gist id, creating one via gh if missing."""
    existing = gist_id()
    if existing:
        return existing

    # Create an empty private gist with gh (one-time).
    readme = Path.home() / ".yt-tui" / "gist-readme.md"
    try:
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text(
            "Private status files for yt-tui OTHER DEVICES.\n",
            encoding="utf-8",
        )
        created = _run(
            [_gh_bin(), "gist", "create", "--private", "-d", "yt-tui device sync", str(readme)],
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _set_error(f"gist create failed: {exc}")
        return None

    if created.returncode != 0:
        _set_error((created.stderr or created.stdout or "gist create failed").strip()[:160])
        return None

    # Output is a URL like https://gist.github.com/<id> or raw id.
    text = (created.stdout or "").strip().splitlines()
    url = text[-1] if text else ""
    new_id = url.rstrip("/").split("/")[-1]
    if not new_id or len(new_id) < 10:
        _set_error("could not parse new gist id")
        return None
    save_gist_id(new_id)
    return new_id


def ensure_repo() -> Path | None:
    """Clone or update the local gist checkout."""
    gist = ensure_gist()
    remote = _remote_url(gist) if gist else None
    if not gist or not remote:
        _set_error("no gist id or GitHub token (gh auth login)")
        return None

    if GIST_REPO_DIR.is_dir() and (GIST_REPO_DIR / ".git").is_dir():
        # Keep remote URL fresh in case the token rotated.
        _run(["git", "remote", "set-url", "origin", remote], cwd=GIST_REPO_DIR)
        return GIST_REPO_DIR

    if GIST_REPO_DIR.exists():
        shutil.rmtree(GIST_REPO_DIR, ignore_errors=True)

    GIST_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    try:
        cloned = _run(
            ["git", "clone", "--depth", "1", remote, str(GIST_REPO_DIR)],
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _set_error(f"clone failed: {exc}")
        return None

    if cloned.returncode != 0:
        _set_error((cloned.stderr or cloned.stdout or "clone failed").strip()[:160])
        return None

    _run(["git", "config", "user.email", "yt-tui@local"], cwd=GIST_REPO_DIR)
    _run(["git", "config", "user.name", "yt-tui"], cwd=GIST_REPO_DIR)
    return GIST_REPO_DIR


def _pull(repo: Path) -> bool:
    global _last_pull
    now = time.time()
    if now - _last_pull < _PULL_MIN_SECONDS:
        return True
    try:
        result = _run(
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            cwd=repo,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _set_error(f"pull failed: {exc}")
        return False
    _last_pull = time.time()
    if result.returncode != 0:
        # main vs master
        result = _run(
            ["git", "pull", "--rebase", "--autostash", "origin", "master"],
            cwd=repo,
            timeout=45,
        )
        if result.returncode != 0:
            _set_error((result.stderr or result.stdout or "pull failed").strip()[:160])
            return False
    return True


def publish_to_gist(filename: str, payload: dict[str, Any]) -> bool:
    """Write one device file and push to the gist."""
    if not _with_lock():
        _set_error("sync lock busy")
        return False
    try:
        repo = ensure_repo()
        if repo is None:
            return False
        if not _pull(repo):
            # Still try to push our file; peer may have diverged only on theirs.
            pass

        path = repo / filename
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _set_error(f"write failed: {exc}")
            return False

        _run(["git", "add", "--", filename], cwd=repo)
        status = _run(["git", "status", "--porcelain", "--", filename], cwd=repo)
        if not (status.stdout or "").strip():
            _set_error("")
            return True  # nothing changed

        commit = _run(
            ["git", "commit", "-m", f"sync {filename}"],
            cwd=repo,
        )
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            _set_error((commit.stderr or commit.stdout or "commit failed").strip()[:160])
            return False

        # Rebase then push; retry once on rejection.
        for _ in range(2):
            _pull(repo)
            pushed = _run(["git", "push", "origin", "HEAD"], cwd=repo, timeout=45)
            if pushed.returncode == 0:
                _set_error("")
                return True
            time.sleep(0.4)

        _set_error((pushed.stderr or pushed.stdout or "push failed").strip()[:160])
        return False
    finally:
        _unlock()


def read_gist_files(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """Read peer JSON files from the local gist checkout (pull first)."""
    if not _with_lock(timeout=3.0):
        # Another publish in progress — read whatever is on disk.
        return _read_repo_files(GIST_REPO_DIR if GIST_REPO_DIR.is_dir() else None)

    try:
        repo = ensure_repo()
        if repo is None:
            return {}
        _pull(repo)
        return _read_repo_files(repo)
    finally:
        _unlock()


def _read_repo_files(repo: Path | None) -> dict[str, dict[str, Any]]:
    if repo is None or not repo.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(repo.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            out[path.name] = parsed
    return out


def gist_status_line() -> str:
    gist = gist_id()
    if not gist:
        return "gist       : none (run: yt-tui --devices init)"
    token = "ok" if github_token() else "no token (gh auth login)"
    err = last_error()
    extra = f" err={err}" if err else ""
    return f"gist       : {gist} (git/{token}){extra}"
