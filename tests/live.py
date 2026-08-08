"""Live network check against a real YouTube stream.

Verifies the worker runs off the main thread without pytchat's
"signal only works in main thread" error.

Run with:  uv run python tests/live.py [url-or-id] [seconds]
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt_tui.chat import ChatEvent, ChatWorker  # noqa: E402
from yt_tui.utils import parse_video_id  # noqa: E402

DEFAULT_TARGET = "jfKfPfyJRdk"  # lofi girl, usually live


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    video_id = parse_video_id(target)
    if not video_id:
        print(f"could not parse: {target}")
        return 1

    print(f"connecting to {video_id} for {seconds:g}s (from a worker thread)")
    events: queue.Queue[ChatEvent] = queue.Queue()
    worker = ChatWorker(video_id, events)
    worker.start()

    deadline = time.time() + seconds
    messages = 0
    statuses: list[str] = []
    errors: list[str] = []

    while time.time() < deadline:
        try:
            event = events.get(timeout=0.5)
        except queue.Empty:
            continue
        if event.kind == "message" and event.message is not None:
            messages += 1
            msg = event.message
            tags = "".join(f"[{label}]" for label, _ in msg.badges)
            extra = f" {msg.amount}" if msg.amount else ""
            if messages <= 12:
                print(f"  {msg.timestamp} {msg.author}{tags}{extra}: {msg.text[:70]}")
        elif event.kind == "status":
            statuses.append(event.text)
            print(f"  <status: {event.text}>")
        else:
            errors.append(f"{event.kind}: {event.text}")
            print(f"  <{event.kind}: {event.text}>")

    worker.stop()
    time.sleep(0.5)

    print()
    print(f"messages received : {messages}")
    print(f"statuses          : {statuses}")
    print(f"errors            : {errors or 'none'}")
    print(f"thread still alive: {worker.running}")

    signal_bug = any("signal only works in main thread" in e for e in errors)
    print(f"signal-thread bug : {'YES (BAD)' if signal_bug else 'no'}")

    if signal_bug:
        return 1
    if not statuses:
        print("\nno status events: could not reach YouTube")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
