#!/bin/bash
# Stop and remove the yt-tui background publisher.

set -euo pipefail

LABEL="com.yt-tui.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

echo "removed $LABEL"
echo "note: this Mac's file stays in the devices folder until it ages out (15 min)."
