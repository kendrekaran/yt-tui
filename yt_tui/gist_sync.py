"""Peer sync over a private GitHub gist.

Uses the GitHub HTTP API with a token from `gh auth token` (cached) so
publishes are ~100ms instead of spawning `gh` for every update.
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

API = "https://api.github.com"
TIMEOUT = 8.0

_token: str | None = None
_token_checked = False
_gist_verified: str | None = None
_client: Any = None

# Short in-process cache so the 1s UI poll does not hit the network twice
# in the same second. Publish always bypasses this.
_read_cache: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})
_READ_CACHE_SECONDS = 0.85


def _http() -> Any:
    """Shared httpx client so TLS sessions stay warm across publishes."""
    global _client
    if _client is None:
        import httpx

        _client = httpx.Client(timeout=TIMEOUT)
    return _client


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
    """Resolve a GitHub token once per process."""
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


def _headers() -> dict[str, str] | None:
    token = github_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "yt-tui",
    }


def ensure_gist() -> str | None:
    """Return the shared gist id, creating one if needed.

    Does not probe the network when an id is already configured.
    """
    global _gist_verified
    existing = gist_id()
    if existing:
        _gist_verified = existing
        return existing

    headers = _headers()
    if headers is None:
        return None

    payload = {
        "description": "yt-tui device sync (auto-updated; safe to ignore)",
        "public": False,
        "files": {
            "README.md": {
                "content": (
                    "Private status files for yt-tui OTHER DEVICES.\n"
                    "Updated automatically by yt-tui.\n"
                )
            }
        },
    }
    try:
        response = _http().post(f"{API}/gists", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None

    new_id = str(data.get("id") or "").strip()
    if not new_id:
        return None
    save_gist_id(new_id)
    _gist_verified = new_id
    return new_id


def publish_to_gist(filename: str, payload: dict[str, Any]) -> bool:
    """Upsert one device JSON file into the shared gist."""
    gist = ensure_gist()
    headers = _headers()
    if not gist or headers is None:
        return False

    body = {
        "files": {
            filename: {"content": json.dumps(payload, ensure_ascii=False, indent=2)}
        }
    }
    try:
        response = _http().patch(f"{API}/gists/{gist}", headers=headers, json=body)
        response.raise_for_status()
    except Exception:
        return False

    global _read_cache
    _read_cache = (0.0, {})
    return True


def read_gist_files(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """Map filename -> parsed JSON payload for every *.json in the gist."""
    global _read_cache
    now = time.time()
    if not force and now - _read_cache[0] < _READ_CACHE_SECONDS:
        return _read_cache[1]

    gist = gist_id()
    headers = _headers()
    if not gist or headers is None:
        return {}

    try:
        response = _http().get(f"{API}/gists/{gist}", headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return _read_cache[1] if _read_cache[1] else {}

    files = data.get("files") or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(files, dict):
        _read_cache = (now, out)
        return out

    for name, meta in files.items():
        if not isinstance(name, str) or not name.endswith(".json"):
            continue
        if not isinstance(meta, dict):
            continue
        content = meta.get("content")
        if (not content) and meta.get("truncated") and meta.get("raw_url"):
            try:
                raw = _http().get(str(meta["raw_url"]), headers=headers)
                raw.raise_for_status()
                content = raw.text
            except Exception:
                continue
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            parsed = json.loads(content)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out[name] = parsed

    _read_cache = (now, out)
    return out


def gist_status_line() -> str:
    gist = gist_id()
    if not gist:
        return "gist       : none (run: yt-tui --devices init)"
    token = "ok" if github_token() else "no token (gh auth login)"
    return f"gist       : {gist} ({token})"
