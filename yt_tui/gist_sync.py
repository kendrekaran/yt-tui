"""Peer sync over a private GitHub gist.

iCloud Drive between two Macs is unreliable here (files publish locally but
never show up on the other machine). A private gist under the same GitHub
account updates in seconds on both sides via the API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Checked into the repo so both Macs share the same gist after `git pull`.
REPO_GIST_FILE = Path(__file__).resolve().parent.parent / "device-sync.gist"
LOCAL_GIST_FILE = Path.home() / ".yt-tui" / "gist-id"


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


def _gh(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Prefer the real gh binary; conda shells sometimes shadow PATH oddly.
    gh = shutil.which("gh") or "/usr/local/bin/gh"
    return subprocess.run(
        [gh, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def ensure_gist() -> str | None:
    """Return an existing gist id, or create a new private one."""
    existing = gist_id()
    if existing:
        # Verify it is reachable; recreate if deleted.
        probe = _gh("api", f"gists/{existing}")
        if probe.returncode == 0:
            return existing

    payload = {
        "description": "yt-tui device sync (auto-updated; safe to ignore)",
        "public": False,
        "files": {
            "README.md": {
                "content": (
                    "Private status files for yt-tui OTHER DEVICES.\n"
                    "Updated automatically by `yt-tui --sync` / the LaunchAgent.\n"
                )
            }
        },
    }
    created = _gh("api", "gists", "--method", "POST", "--input", "-", input_text=json.dumps(payload))
    if created.returncode != 0:
        return None
    try:
        data = json.loads(created.stdout)
    except ValueError:
        return None
    new_id = str(data.get("id") or "").strip()
    if not new_id:
        return None
    save_gist_id(new_id)
    return new_id


def publish_to_gist(filename: str, payload: dict[str, Any]) -> bool:
    """Upsert one device JSON file into the shared gist."""
    gist = ensure_gist()
    if not gist:
        return False
    body = {
        "files": {
            filename: {"content": json.dumps(payload, ensure_ascii=False, indent=2)}
        }
    }
    result = _gh(
        "api",
        f"gists/{gist}",
        "--method",
        "PATCH",
        "--input",
        "-",
        input_text=json.dumps(body),
    )
    return result.returncode == 0


def read_gist_files() -> dict[str, dict[str, Any]]:
    """Map filename -> parsed JSON payload for every *.json in the gist."""
    gist = gist_id()
    if not gist:
        return {}
    result = _gh("api", f"gists/{gist}")
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    files = data.get("files") or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(files, dict):
        return out
    for name, meta in files.items():
        if not isinstance(name, str) or not name.endswith(".json"):
            continue
        if not isinstance(meta, dict):
            continue
        content = meta.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            parsed = json.loads(content)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out[name] = parsed
    return out


def gist_status_line() -> str:
    gist = gist_id()
    if not gist:
        return "gist       : none (will create on next publish if `gh` is logged in)"
    return f"gist       : {gist}"
