"""Dump the rendered screen as text so layout can be eyeballed in CI/logs.

Run with:  uv run python tests/preview.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.smoke import sample_messages  # noqa: E402
from yt_tui.app import YtTuiApp  # noqa: E402
from yt_tui.chat import ChatEvent  # noqa: E402


async def main() -> None:
    app = YtTuiApp(publish=False)
    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause()
        for message in sample_messages():
            app.events.put(ChatEvent(kind="message", message=message))
        app.events.put(ChatEvent(kind="status", text="live"))
        app.video_id = "jfKfPfyJRdk"
        await pilot.pause()
        await asyncio.sleep(0.5)
        await pilot.pause()

        for strip in app.screen._compositor.render_strips():  # noqa: SLF001
            print("".join(seg.text for seg in strip).rstrip())

        out = Path(__file__).resolve().parents[1] / "preview.svg"
        app.save_screenshot(str(out))
        print(f"\nsaved: {out}")


if __name__ == "__main__":
    asyncio.run(main())
