"""Headless smoke test: boots the TUI, injects fake chat, checks rendering.

Run with:  uv run python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt_tui.app import YtTuiApp  # noqa: E402
from yt_tui.chat import (  # noqa: E402
    MSG_MEMBER,
    MSG_STICKER,
    MSG_SUPERCHAT,
    MSG_TEXT,
    ChatEvent,
    ChatMessage,
)
from yt_tui.utils import author_color, parse_video_id  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({extra})" if extra and not condition else ""))
    if not condition:
        FAILURES.append(label)


def test_parsing() -> None:
    print("\nvideo id parsing")
    cases = {
        "https://www.youtube.com/watch?v=jfKfPfyJRdk": "jfKfPfyJRdk",
        "https://youtu.be/jfKfPfyJRdk?t=30": "jfKfPfyJRdk",
        "https://www.youtube.com/live/jfKfPfyJRdk": "jfKfPfyJRdk",
        "https://www.youtube.com/embed/jfKfPfyJRdk": "jfKfPfyJRdk",
        "https://www.youtube.com/shorts/jfKfPfyJRdk": "jfKfPfyJRdk",
        "youtube.com/watch?v=jfKfPfyJRdk&list=xyz": "jfKfPfyJRdk",
        "jfKfPfyJRdk": "jfKfPfyJRdk",
        "  jfKfPfyJRdk  ": "jfKfPfyJRdk",
    }
    for raw, expected in cases.items():
        got = parse_video_id(raw)
        check(f"{raw[:46]:46} -> {expected}", got == expected, f"got {got}")
    check("garbage rejected", parse_video_id("not a video") is None)
    check("empty rejected", parse_video_id("") is None)
    check(
        "author color is stable",
        author_color("bob", "c1") == author_color("bob", "c1"),
    )


def sample_messages() -> list[ChatMessage]:
    return [
        ChatMessage(
            msg_id="1",
            kind=MSG_TEXT,
            author="normal_viewer",
            color=author_color("normal_viewer"),
            timestamp="05:04",
            text="hello from the chat! [not markup] <ok>",
        ),
        ChatMessage(
            msg_id="2",
            kind=MSG_TEXT,
            author="the_streamer",
            color=author_color("the_streamer"),
            timestamp="05:05",
            text="thanks for tuning in",
            is_owner=True,
        ),
        ChatMessage(
            msg_id="3",
            kind=MSG_TEXT,
            author="helpful_mod",
            color=author_color("helpful_mod"),
            timestamp="05:05",
            text="keep it civil please",
            is_moderator=True,
        ),
        ChatMessage(
            msg_id="4",
            kind=MSG_SUPERCHAT,
            author="big_spender",
            color=author_color("big_spender"),
            timestamp="05:06",
            text="love the stream, keep going",
            amount="\u20b9500.00",
            currency="INR",
            is_member=True,
        ),
        ChatMessage(
            msg_id="5",
            kind=MSG_STICKER,
            author="sticker_fan",
            color=author_color("sticker_fan"),
            timestamp="05:07",
            text="sent a Super Sticker",
            amount="$2.00",
        ),
        ChatMessage(
            msg_id="6",
            kind=MSG_MEMBER,
            author="brand_new",
            color=author_color("brand_new"),
            timestamp="05:08",
            text="welcome to the channel!",
            is_member=True,
        ),
    ]


async def test_app() -> None:
    print("\nTUI (headless)")
    app = YtTuiApp(publish=False)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        check("app booted, CSS parsed", True)

        for message in sample_messages():
            app.events.put(ChatEvent(kind="message", message=message))
        app.events.put(ChatEvent(kind="status", text="live"))
        await pilot.pause()
        await asyncio.sleep(0.4)
        await pilot.pause()

        check("all messages rendered", app.message_count == 6, f"{app.message_count}")

        visible = _screen_text(app)

        check("chat text visible", "hello from the chat!" in visible)
        check("markup in message escaped", "[not markup]" in visible)
        check("OWNER badge", "OWNER" in visible)
        check("MOD badge", "MOD" in visible)
        check("super chat amount", "500.00" in visible)
        check("super sticker", "2.00" in visible)
        check("new member line", "welcome to the channel!" in visible)
        check("LIVE indicator", "LIVE" in visible)

        check("section THIS MAC", "THIS MAC" in visible)
        check("section OTHER DEVICES", "OTHER DEVICES" in visible)
        check("section CLOUD", "CLOUD" in visible)
        check("section SHELLS", "SHELLS" in visible)
        check(
            "this agent shows as running",
            "RUN" in visible,
            "expected a running local agent",
        )

        # Hotkeys only apply when the URL input does not have focus.
        app.query_one("#chat-log").focus()
        await pilot.pause()

        await pilot.press("p")
        await pilot.pause()
        check("pause marker", "paused" in _screen_text(app).lower())
        check("paused flag set", app.paused is True)

        await pilot.press("p")
        await pilot.pause()
        check("resumed", app.paused is False)

        await pilot.press("c")
        await pilot.pause()
        check("clear empties chat", app.message_count == 0)

        await pilot.press("r")
        await pilot.pause()
        check("refresh key handled", True)

        app.events.put(ChatEvent(kind="error", text="offline test error"))
        await pilot.pause()
        await asyncio.sleep(0.3)
        await pilot.pause()
        check("error status", app.status == "error")

        # Typing in the input must not trigger the q/p/c/r hotkeys.
        app.query_one("#url-input").focus()
        await pilot.pause()
        await pilot.press("q", "p", "c")
        await pilot.pause()
        check("typing does not quit", app.is_running)
        check(
            "typed characters land in the input",
            "qpc" in app.query_one("#url-input").value,
            app.query_one("#url-input").value,
        )

        await pilot.press("enter")
        await pilot.pause()
        check("invalid id reports error", app.status == "error")


def _screen_text(app: YtTuiApp) -> str:
    lines = []
    for strip in app.screen._compositor.render_strips():  # noqa: SLF001
        lines.append("".join(seg.text for seg in strip))
    return "\n".join(lines)


def main() -> int:
    test_parsing()
    asyncio.run(test_app())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
