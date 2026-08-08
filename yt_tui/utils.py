"""Shared helpers: video id parsing, author colors, badges, time formatting."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# /live/<id>, /embed/<id>, /shorts/<id>, /v/<id>
_PATH_PREFIXES = ("live", "embed", "shorts", "v")


def parse_video_id(raw: str) -> str | None:
    """Extract an 11-char YouTube video id from a URL or bare id."""
    if not raw:
        return None
    text = raw.strip().strip("\"'")
    if not text:
        return None

    if _VIDEO_ID_RE.match(text):
        return text

    candidate = text if "//" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [p for p in (parsed.path or "").split("/") if p]

    if host in {"youtu.be", "y2u.be"} and parts:
        return _clean(parts[0])

    if "youtube" in host or host.endswith("youtube-nocookie.com"):
        if parsed.query:
            values = parse_qs(parsed.query).get("v")
            if values:
                found = _clean(values[0])
                if found:
                    return found
        if parts:
            if parts[0] in _PATH_PREFIXES and len(parts) > 1:
                return _clean(parts[1])
            # Bare /<id> style links.
            return _clean(parts[-1])

    # Last resort: any 11-char token inside the string.
    for token in re.split(r"[^A-Za-z0-9_-]+", text):
        if _VIDEO_ID_RE.match(token):
            return token
    return None


def _clean(value: str) -> str | None:
    token = value.split("?")[0].split("&")[0].strip()
    return token if _VIDEO_ID_RE.match(token) else None


# Vivid-but-readable palette in the spirit of YouTube chat usernames.
AUTHOR_COLORS: tuple[str, ...] = (
    "#5eb0ff",
    "#7bd88f",
    "#ffb454",
    "#ff8a80",
    "#c792ea",
    "#4dd0e1",
    "#f78fb3",
    "#a5d6a7",
    "#ffd166",
    "#9fa8ff",
    "#6ee7c7",
    "#ff9e64",
    "#8ecae6",
    "#e6b0ff",
    "#b8e986",
    "#f4a3c0",
)


def author_color(name: str, channel_id: str = "") -> str:
    """Stable per-author color so the same person keeps the same hue."""
    seed = (channel_id or name or "anon").encode("utf-8", "replace")
    digest = hashlib.md5(seed, usedforsecurity=False).digest()
    return AUTHOR_COLORS[digest[0] % len(AUTHOR_COLORS)]


def short_id(value: str, length: int = 8) -> str:
    text = (value or "").strip()
    return text[:length] if text else "?"


def humanize_age(epoch: float, now: float | None = None) -> str:
    """Render a timestamp as a compact relative age: '3m ago'."""
    if not epoch:
        return "unknown"
    delta = max(0.0, (now if now is not None else time.time()) - epoch)
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def truncate(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "\u2026"


def project_label(path_name: str) -> str:
    """Turn a Cursor project dir name into something readable.

    'Users-alex-Desktop-work-yt-tui' -> 'yt-tui'
    """
    name = (path_name or "").strip()
    if not name:
        return "unknown"

    home_slug = "Users-" + Path.home().name
    if name == home_slug or name == "Users":
        return "~"
    if name.startswith(home_slug + "-"):
        name = name[len(home_slug) + 1 :]
    elif name.startswith("Users-"):
        name = name[len("Users-") :]

    parts = [p for p in name.split("-") if p]
    if not parts:
        return "~"
    # Keep the trailing 1-2 segments, which are usually the repo name.
    tail = parts[-2:] if len(parts) > 1 else parts
    generic = ("Desktop", "work", "Documents", "Developer", "src", Path.home().name)
    if len(tail) > 1 and tail[0] in generic:
        tail = tail[1:]
    return "-".join(tail)
