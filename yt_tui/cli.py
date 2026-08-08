"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt-tui",
        description="YouTube live chat and live Cursor agent activity, side by side.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="YouTube live URL or 11-character video id",
    )
    parser.add_argument(
        "--devices",
        nargs="?",
        const="status",
        choices=["status", "init"],
        help="'--devices' prints sync status; '--devices init' creates the folder",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="publish this machine's agent activity once, without opening the TUI",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="with --sync, keep publishing every few seconds",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between --sync --loop publishes (default: 5)",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="do not publish this machine's activity while the TUI runs",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="with --sync --loop, only log failures (keeps background logs small)",
    )
    parser.add_argument("--version", action="version", version=f"yt-tui {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from . import devices

    if args.devices == "init":
        target = devices.ensure_dir()
        if target is None:
            print("Could not create the devices folder.", file=sys.stderr)
            return 1
        path = devices.publish()
        print(f"devices folder: {target}")
        print(f"published     : {path if path else 'failed'}")
        print()
        print(devices.status_report())
        return 0

    if args.devices == "status":
        print(devices.status_report())
        return 0

    if args.sync:
        if not args.loop:
            path = devices.publish()
            print(f"published: {path if path else 'failed'}")
            return 0 if path else 1

        interval = max(1.0, args.interval)
        print(
            f"publishing as '{devices.device_name()}' every {interval:g}s "
            f"to {devices.devices_dir()}"
        )
        print("press ctrl-c to stop")
        previous_ok: bool | None = None
        try:
            while True:
                ok = devices.publish() is not None
                # In quiet mode only report transitions, so a long-running
                # LaunchAgent does not grow an enormous log.
                if not args.quiet or ok != previous_ok:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{stamp}] {'ok' if ok else 'failed'}", flush=True)
                previous_ok = ok
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0

    from .app import run

    run(target=args.target, publish=not args.no_publish)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
