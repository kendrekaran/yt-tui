"""Turn YouTube chat shortcodes into glyphs the terminal can show.

pytchat puts emoji into `message` as `:shortcode:` text (and into
`messageEx` as `{txt, id, url}` dicts). Unicode shortcodes expand via the
`emoji` package; YouTube's own coloured-face set is mapped by hand.
"""

from __future__ import annotations

import re
from typing import Any

# YouTube's built-in live-chat emoji (not in Unicode). Closest glyph for each.
_YOUTUBE_CUSTOM: dict[str, str] = {
    "hand-pink-waving": "👋",
    "hand-orange-pointing": "👉",
    "hand-orange-writing": "✍️",
    "hand-purple-rock": "🤘",
    "hand-purple-wrestling": "🤼",
    "hand-turquoise-covering-eyes": "🙈",
    "hand-turquoise-raising": "🙋",
    "face-turquoise-covering-eyes": "🙈",
    "face-blue-smiling": "😊",
    "face-blue-wide-eyes": "😮",
    "face-blue-frowning": "😟",
    "face-blue-laughing": "😁",
    "face-blue-sweating": "😅",
    "face-blue-tongue": "😛",
    "face-blue-speechless": "😶",
    "face-blue-tears": "😢",
    "face-blue-thinking": "🤔",
    "face-blue-sleeping": "😴",
    "face-blue-yawning": "🥱",
    "face-blue-star-eyes": "🤩",
    "face-purple-smiling": "🙂",
    "face-purple-crying": "😢",
    "face-purple-angry": "😠",
    "face-purple-raised-eyebrow": "🤨",
    "face-red-heart-shape": "❤️",
    "face-red-droopy-eyes": "😞",
    "face-red-droopy-eyes-flushed": "😳",
    "face-red-smiling": "☺️",
    "face-red-surprised": "😲",
    "face-red-angry": "😡",
    "face-orange-drooling": "🤤",
    "face-orange-festive": "🥳",
    "face-pink-smiling": "😊",
    "face-pink-tears": "🥹",
    "face-pink-heart": "🥰",
    "eyes-purple-crying": "😭",
    "eyes-orange-wide": "👀",
    "eyes-turquoise-looking": "👀",
    "mouth-turquoise-open": "😮",
    "heart-pink-growing": "💗",
    "heart-red-broken": "💔",
    "heart-red-gift": "💝",
    "person-turquoise-playing": "🎮",
    "person-blue-running": "🏃",
    "person-orange-dancing": "💃",
    "person-pink-hugging": "🤗",
    "yougotthis": "💪",
    "whoops": "🤦",
    "ohmy": "😱",
    "yay": "🎉",
    "thanks": "🙏",
    "thinking-face": "🤔",
}

_SHORTCODE_RE = re.compile(r":([A-Za-z0-9_+\-]+):")
_CODEPOINT_RE = re.compile(r"^U\+([0-9A-Fa-f]+)(?:-U\+([0-9A-Fa-f]+))*$")


def expand_emoji_text(text: str) -> str:
    """Replace every `:shortcode:` in a string with a glyph when possible."""
    if not text or ":" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        return shortcode_to_glyph(match.group(1))

    return _SHORTCODE_RE.sub(_replace, text)


def shortcode_to_glyph(name: str) -> str:
    """Map a shortcode body (no colons) to a single glyph or a compact fallback."""
    key = (name or "").strip().strip(":")
    if not key:
        return ""

    # Leading underscore shows up on some YouTube custom names.
    cleaned = key.lstrip("_").lower()

    if cleaned in _YOUTUBE_CUSTOM:
        return _YOUTUBE_CUSTOM[cleaned]

    # Hyphenated YouTube names sometimes mirror Unicode aliases with underscores.
    for candidate in (cleaned, cleaned.replace("-", "_"), cleaned.replace("_", "-")):
        glyph = _emojize_alias(candidate)
        if glyph:
            return glyph

    # Unknown custom emoji: keep a readable chip instead of a raw :name:.
    label = cleaned.replace("_", "-")
    if len(label) > 18:
        label = label[:17] + "…"
    return f"[{label}]"


def _emojize_alias(name: str) -> str | None:
    try:
        import emoji
    except ImportError:
        return None
    token = f":{name}:"
    for language in ("alias", "en"):
        try:
            result = emoji.emojize(token, language=language)
        except Exception:
            continue
        if result and result != token:
            return result
    return None


def glyph_from_emoji_id(emoji_id: str) -> str | None:
    """Turn a YouTube emojiId like 'U+1F602' into a character when possible."""
    text = (emoji_id or "").strip()
    if not text:
        return None
    # Channel custom ids look like '<channelId>/<customId>'.
    if "/" in text:
        return None
    if _CODEPOINT_RE.match(text.replace(",", "-")):
        parts = re.findall(r"U\+([0-9A-Fa-f]+)", text)
        try:
            return "".join(chr(int(p, 16)) for p in parts)
        except ValueError:
            return None
    # Plain alias-style id.
    return shortcode_to_glyph(text) if text else None


def compose_chat_text(item: Any) -> str:
    """Build display text from a pytchat chat item, preferring real glyphs."""
    message_ex = getattr(item, "messageEx", None)
    if isinstance(message_ex, list) and message_ex:
        parts: list[str] = []
        for part in message_ex:
            if isinstance(part, str):
                parts.append(expand_emoji_text(part))
                continue
            if not isinstance(part, dict):
                parts.append(str(part))
                continue

            emoji_id = str(part.get("id") or "")
            from_id = glyph_from_emoji_id(emoji_id)
            if from_id and from_id != emoji_id and not from_id.startswith("["):
                parts.append(from_id)
                continue

            txt = str(part.get("txt") or "")
            if txt.startswith(":") and txt.endswith(":") and len(txt) > 2:
                parts.append(shortcode_to_glyph(txt[1:-1]))
            elif txt:
                parts.append(expand_emoji_text(txt))
            elif from_id:
                parts.append(from_id)
        return "".join(parts)

    return expand_emoji_text(str(getattr(item, "message", "") or ""))
