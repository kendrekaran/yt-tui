#!/bin/bash
# Install a LaunchAgent that publishes this Mac's Cursor activity on login,
# so it shows up under OTHER DEVICES on your other Macs even when the TUI
# is closed.
#
# Usage:  ./scripts/install-launchagent.sh [interval-seconds]
#
# Paths are resolved at install time, so the same script works on any Mac.

set -euo pipefail

LABEL="com.yt-tui.sync"
INTERVAL="${1:-5}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/yt-tui-sync.log"

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

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Make sure dependencies exist before launchd starts relying on them.
( cd "$PROJECT_DIR" && "$UV_BIN" sync --quiet )

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

# Replace any previous instance, ignoring "not loaded" errors.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

sleep 2
echo
if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  echo "installed and running: $LABEL"
  echo "it will start automatically on every login"
else
  echo "warning: agent did not report as running; check $LOG" >&2
  exit 1
fi

echo
echo "check status : uv run yt-tui --devices"
echo "view log     : tail -f $LOG"
echo "remove       : ./scripts/uninstall-launchagent.sh"
