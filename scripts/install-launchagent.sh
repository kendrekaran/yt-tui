#!/bin/bash
# Install a LaunchAgent that publishes this Mac's Cursor activity on login,
# so it shows up under OTHER DEVICES on your other Macs even when the TUI
# is closed.
#
# Usage:  ./scripts/install-launchagent.sh [interval-seconds]
#
# Paths are resolved at install time, so the same script works on any Mac.

set -uo pipefail

LABEL="com.yt-tui.sync"
INTERVAL="${1:-0.5}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/yt-tui-sync.log"
DOMAIN="gui/$(id -u)"

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then UV_BIN="$candidate"; break; fi
  done
fi
if [ -z "$UV_BIN" ]; then
  echo "error: could not find 'uv'. Install it first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

echo "project : $PROJECT_DIR"
echo "uv      : $UV_BIN"
echo "interval: ${INTERVAL}s"
echo "log     : $LOG"
echo

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Make sure dependencies exist before launchd starts relying on them.
( cd "$PROJECT_DIR" && "$UV_BIN" sync --quiet ) || {
  echo "error: 'uv sync' failed in $PROJECT_DIR" >&2
  exit 1
}

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>yt-tui</string>
        <string>--sync</string>
        <string>--loop</string>
        <string>--quiet</string>
        <string>--interval</string>
        <string>$INTERVAL</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>$LOG</string>

    <key>StandardErrorPath</key>
    <string>$LOG</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$UV_BIN"):/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
PLIST_EOF

if ! plutil -lint "$PLIST" >/dev/null; then
  echo "error: generated plist is invalid:" >&2
  plutil -lint "$PLIST" >&2
  exit 1
fi

# Remove any previous copy, then clear a "disabled" override, which is the
# usual reason bootstrap fails with an I/O error.
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
launchctl unload "$PLIST" >/dev/null 2>&1
launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1
sleep 1

bootstrap_error="$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1)"
bootstrap_status=$?

if [ $bootstrap_status -ne 0 ]; then
  echo "launchctl bootstrap failed (exit $bootstrap_status): ${bootstrap_error:-no message}"
  echo "retrying with the legacy loader..."
  load_error="$(launchctl load -w "$PLIST" 2>&1)"
  if [ $? -ne 0 ]; then
    echo "launchctl load also failed: ${load_error:-no message}" >&2
  fi
fi

# Force a start even if RunAtLoad was ignored.
launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1
sleep 2

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  pid="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3; exit}')"
  echo
  echo "installed: $LABEL${pid:+ (pid $pid)}"
  echo "it will start automatically on every login"
  echo
  echo "check status : cd $PROJECT_DIR && uv run yt-tui --devices"
  echo "view log     : tail -f $LOG"
  echo "remove       : ./scripts/uninstall-launchagent.sh"
  exit 0
fi

cat >&2 <<HINT

The LaunchAgent could not be started. This is only a convenience wrapper,
so you can publish manually in the meantime:

    cd $PROJECT_DIR && uv run yt-tui --sync --loop

Things worth checking:
  - full error:   launchctl bootstrap $DOMAIN "$PLIST"
  - service log:  cat "$LOG"
  - if it says the service is disabled:
        launchctl enable $DOMAIN/$LABEL
        ./scripts/install-launchagent.sh
  - over SSH, launchd may refuse a GUI domain; run it in Terminal.app
HINT
exit 1
