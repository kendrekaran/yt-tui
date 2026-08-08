# yt-tui

A split-pane terminal app: **YouTube live chat on the left, live Cursor agent activity on the right.**

The left pane is styled after YouTube's live chat panel (dark `#0f0f0f` background, red LIVE pill,
per-author colors, Super Chat cards, mod/owner/member badges). The right pane is styled after
Cursor's agent sidebar (workbench grays, blue accents, Composer-style cards, uppercase section
labels). The two halves are meant to look like two different products side by side.

```
┌─ ▶ yt-tui ─┬─ https://youtube.com/watch?v=… ──────────┬── ● LIVE 128 ──┐
│ Live chat  ● LIVE  128 messages          │ CURSOR ACTIVITY             │
│ 05:04 someone  MOD  hey chat             │ THIS MAC                    │
│ ┃ 05:06 bigfan  MEMBER  ₹500.00          │ ┃ ● RUN  Build yt-tui       │
│ ┃ love the stream                        │ ┃ Shell · todos 3/10        │
│ 05:08 ★ newbie became a new member       │ ┃ ► write the README        │
│                                          │ OTHER DEVICES / CLOUD       │
└──────────────────────────────────────────┴─────────────────────────────┘
  p Pause   c Clear   r Refresh   i URL   q Quit
```

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- macOS for the Cursor panel (it reads `~/.cursor` and `~/Library/…`); the chat pane works anywhere

No YouTube API key and no Selenium: chat is read through [`pytchat-ng`](https://pypi.org/project/pytchat-ng/).

## Install and run

```bash
uv sync
uv run yt-tui                                          # open the TUI, paste a URL in the bar
uv run yt-tui https://www.youtube.com/watch?v=VIDEOID  # open already connected
uv run yt-tui VIDEOID                                  # bare 11-char id also works
```

Accepted forms: `watch?v=`, `youtu.be/`, `/live/`, `/embed/`, `/shorts/`, or a bare 11-character id.

To install it as a normal command:

```bash
uv tool install .
yt-tui <url-or-id>
```

## Keys

| Key | Action |
| --- | --- |
| `Enter` | connect to the URL in the toolbar |
| `p` | pause / resume chat rendering (messages queue up, nothing is lost) |
| `c` | clear the chat pane |
| `r` | force-refresh the Cursor pane |
| `i` | jump to the URL bar |
| `q` | quit |

Hotkeys are ignored while the URL bar has focus, so you can type freely. Press `Enter` or
`Escape` to leave the bar.

## What the right pane shows

Refreshes every 1.5s on a worker thread, so the chat never stutters.

- **THIS MAC** — local agents parsed from `~/.cursor/projects/*/agent-transcripts/*/*.jsonl`.
  Each card shows a status pill (`● RUN` / `✓ DONE` / `idle`), the task (the last `<user_query>`
  in the transcript), what it is doing right now (recent tools, todo progress, or
  `finished (success)`), any `TodoWrite` checklist (`✓` done, `►` in progress, `·` pending),
  and a meta row with project, short agent id, and age.
- **OTHER DEVICES** — agents from your other Macs (see below).
- **CLOUD** — Cursor Background/Cloud agents.
- **SHELLS** — recent terminals from `~/.cursor/projects/*/terminals/*.txt`.

An agent counts as running when its transcript has no trailing `turn_ended` event and the file
was touched in the last 15 minutes; otherwise it is done or idle.

## Cross-device sync (second MacBook)

Local IDE chats do not sync through your Cursor account, so yt-tui uses a shared folder instead.
Each machine writes `<device>.json` and reads its peers' files.

Resolution order for the folder:

1. `YT_TUI_DEVICES_DIR` if set
2. `~/Library/Mobile Documents/com~apple~CloudDocs/yt-tui-devices` (iCloud Drive)
3. `~/.yt-tui/devices`

Set up **on both Macs**:

```bash
yt-tui --devices init     # create the folder and publish once
yt-tui --devices          # show sync status and detected peers
```

The TUI publishes this machine's activity every 5s while it runs. If you want the other Mac to
appear without keeping the TUI open there, run a publisher on it:

```bash
yt-tui --sync             # publish once
yt-tui --sync --loop      # keep publishing (this is what the other Mac should run)
```

Peer files older than 15 minutes are ignored, and a machine never lists itself.

### Publish automatically on login

Rather than remembering to start `--sync --loop`, install a LaunchAgent on each Mac:

```bash
./scripts/install-launchagent.sh          # every 5s (pass a number to change it)
tail -f ~/Library/Logs/yt-tui-sync.log    # watch it
./scripts/uninstall-launchagent.sh        # stop and remove
```

It resolves `uv` and the project path at install time, so the same script works on any Mac, and
it restarts itself on login. In this mode the publisher logs only state changes, so the log stays
a few lines long instead of growing all day.

```bash
export YT_TUI_DEVICE_NAME="studio"   # label this machine (defaults to hostname)
export YT_TUI_DEVICES_DIR="~/Dropbox/yt-tui-devices"   # use something other than iCloud
```

Useful flags: `--no-publish` (read peers but stay invisible), `--interval N` (seconds between
`--sync --loop` publishes).

## Cloud agents (optional)

With an API key, yt-tui calls `GET https://api.cursor.com/v1/agents` using Basic auth with the
key as the username:

```bash
export CURSOR_API_KEY="key_..."
```

The key is only ever read from the environment; nothing is written to disk. Without a key,
yt-tui falls back to Cursor's local cache at
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
(keys matching `cloudAgentRepository.agents%`).

Results are cached for 20s so the 1.5s UI refresh does not hammer the network, and only running
agents plus anything finished in the last 24 hours are shown. If there is no key, no cache, or no
network, the section reads `none` with a short reason in the footer (`key rejected`, `offline`,
`no key / no cache`) instead of failing.

## Development

```bash
uv run python tests/smoke.py    # headless: renders the UI, checks parsing, badges, keys
uv run python tests/preview.py  # dump the rendered screen as text + preview.svg
uv run python tests/live.py     # hit a real live stream for 20s
```

`tests/live.py` takes an optional video id and duration: `uv run python tests/live.py VIDEOID 30`.

Note: if your shell sets `NO_COLOR=1` or `TERM=dumb`, previews render in grayscale. Force color
with `env -u NO_COLOR TERM=xterm-256color uv run python tests/preview.py`.

## Layout

```
yt_tui/
  cli.py            # argument parsing and the --devices / --sync entry points
  app.py            # Textual app, CSS, both panes
  chat.py           # pytchat worker thread and the queue the UI drains
  cursor_status.py  # transcript and terminal parsing, snapshot merge
  cloud_agents.py   # cloud agents via API or local SQLite cache
  devices.py        # shared-folder mesh for other Macs
  utils.py          # video ids, author colors, time formatting
```

## Notes and limitations

- pytchat is always created with `interruptable=False`; its signal handlers only work on the main
  thread and would otherwise raise `signal only works in main thread` from the worker.
- Chat scrollback is capped at 300 rows.
- Super Stickers show the amount and a placeholder line, since images cannot render in a terminal.
- Emoji in messages arrive as pytchat shortcodes, e.g. `:smile:`.
- A stream whose chat has ended reports `Ended` rather than an error.
