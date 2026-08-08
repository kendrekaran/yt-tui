"""Textual application: YouTube live chat (left) + Cursor activity (right)."""

from __future__ import annotations

import queue
import time
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static
from textual.worker import Worker

from . import devices
from .chat import (
    MSG_MEMBER,
    MSG_STICKER,
    MSG_SUPERCHAT,
    ChatEvent,
    ChatMessage,
    ChatWorker,
)
from .cursor_status import (
    DONE,
    RUNNING,
    AgentInfo,
    ShellInfo,
    Snapshot,
    TodoItem,
    agent_handle,
    build_snapshot,
)
from .utils import humanize_age, parse_video_id, truncate

MAX_CHAT_ROWS = 300
CURSOR_REFRESH_SECONDS = 1.5
PUBLISH_EVERY_SECONDS = 5.0

# Muted grays and the YouTube/Cursor accents used in inline markup.
DIM = "#6f6f6f"
BODY = "#e9e9e9"
CURSOR_MUTED = "#858585"
CURSOR_ACCENT = "#4fc1e9"


class ChatRow(Static):
    """One rendered live-chat message."""

    def __init__(self, message: ChatMessage) -> None:
        classes = "chat-row"
        if message.kind == MSG_SUPERCHAT:
            classes += " superchat"
        elif message.kind == MSG_STICKER:
            classes += " supersticker"
        elif message.kind == MSG_MEMBER:
            classes += " newmember"
        super().__init__(self._build(message), classes=classes, markup=True)

    @staticmethod
    def _badges(message: ChatMessage) -> str:
        colors = {
            "OWNER": "black on #ffd600",
            "MOD": "white on #5e84f1",
            "MEMBER": "white on #2ba640",
            "VERIFIED": f"black on {DIM}",
        }
        out = []
        for label, _css in message.badges:
            style = colors.get(label, f"black on {DIM}")
            out.append(f"[{style}] {label} [/]")
        return "".join(out)

    def _build(self, message: ChatMessage) -> str:
        name = escape(message.author)
        body = escape(message.text)
        badges = self._badges(message)
        stamp = f"[{DIM}]{escape(message.timestamp)}[/]"
        author = f"[b {message.color}]{name}[/]"

        if message.kind == MSG_SUPERCHAT:
            amount = escape(message.amount or message.currency or "SUPER CHAT")
            head = f"{stamp} {author} {badges}[black on #ffca28] {amount} [/]"
            return f"{head}\n[b #ffe082]{body}[/]" if body else head

        if message.kind == MSG_STICKER:
            amount = escape(message.amount or "SUPER STICKER")
            head = f"{stamp} {author} {badges}[black on #00b8d4] {amount} [/]"
            return f"{head}\n[#80deea]{body}[/]"

        if message.kind == MSG_MEMBER:
            return f"{stamp} [#2ba640]\u2605[/] {author} [#7bd88f]{body}[/]"

        sep = " " if badges else ""
        return f"{stamp} {author}{sep}{badges} [{BODY}]{body}[/]"


class AgentCard(Static):
    """Composer-style card for one agent."""

    def __init__(self, agent: AgentInfo, now: float, width: int = 44) -> None:
        classes = f"agent-card agent-{agent.status}"
        self.card_width = max(20, width)
        super().__init__(self._build(agent, now), classes=classes, markup=True)

    @staticmethod
    def _pill(status: str) -> str:
        if status == RUNNING:
            return "[b #2ea043]\u25cf RUN[/]"
        if status == DONE:
            return f"[{CURSOR_MUTED}]\u2713 DONE[/]"
        return f"[{DIM}]idle[/]"

    def _build(self, agent: AgentInfo, now: float) -> str:
        # The pane is narrow, so every line starts at the left edge and we
        # lean on the card border for grouping rather than indentation.
        width = self.card_width
        pill = self._pill(agent.status)
        # "● RUN " costs 6 columns on the title line.
        title = escape(truncate(agent.task, max(12, width * 2 - 8)))
        lines = [f"{pill} [#e4e4e4]{title}[/]"]

        if agent.detail:
            lines.append(f"[{CURSOR_MUTED}]{escape(truncate(agent.detail, width))}[/]")

        window = _todo_window(agent.todos)
        for todo in window:
            if todo.status == "completed":
                style = CURSOR_MUTED
            elif todo.status == "in_progress":
                style = CURSOR_ACCENT
            else:
                style = DIM
            text = escape(truncate(todo.content, max(8, width - 2)))
            lines.append(f"[{style}]{todo.glyph} {text}[/]")

        extra = len(agent.todos) - len(window)
        if extra > 0:
            lines.append(f"[{DIM}]  +{extra} more[/]")

        meta = f"{agent_handle(agent)} \u00b7 {humanize_age(agent.updated, now)}"
        lines.append(f"[{DIM}]{escape(truncate(meta, width))}[/]")
        return "\n".join(lines)


def _todo_window(todos: list[TodoItem], size: int = 4) -> list[TodoItem]:
    """Show a window around the active item rather than always the first few."""
    if len(todos) <= size:
        return todos
    active = next(
        (i for i, t in enumerate(todos) if t.status == "in_progress"),
        None,
    )
    if active is None:
        active = next(
            (i for i, t in enumerate(todos) if t.status != "completed"), 0
        )
    start = max(0, min(active - 1, len(todos) - size))
    return todos[start : start + size]


class ShellRow(Static):
    def __init__(self, shell: ShellInfo, now: float, width: int = 44) -> None:
        self.card_width = max(20, width)
        super().__init__(self._build(shell, now), classes="shell-row", markup=True)

    def _build(self, shell: ShellInfo, now: float) -> str:
        width = self.card_width
        command = escape(truncate(shell.command, max(12, width * 2 - 4)))
        meta_bits = [b for b in (shell.device, _shorten_path(shell.cwd)) if b]
        meta = escape(truncate(" \u00b7 ".join(meta_bits), max(10, width - 12)))
        # A file can be left claiming "running" long after the shell died.
        state = (shell.status or "").lower()
        fresh = (now - shell.updated) < 300
        marker = "[#2ea043]\u25cf[/] " if state == "running" and fresh else ""
        line = f"[{CURSOR_ACCENT}]$[/] {marker}[#d4d4d4]{command}[/]"
        return f"{line}\n  [{DIM}]{meta} \u00b7 {humanize_age(shell.updated, now)}[/]"


class SectionLabel(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"[{CURSOR_MUTED}]{escape(text)}[/]", classes="section-label")


class EmptyRow(Static):
    def __init__(self, text: str = "none") -> None:
        super().__init__(f"[{DIM}]{escape(text)}[/]", classes="empty-row")


class YtTuiApp(App[None]):
    """Split-pane TUI."""

    TITLE = "yt-tui"
    SUB_TITLE = "YouTube live chat + Cursor agents"

    BINDINGS = [
        ("p", "toggle_pause", "Pause"),
        ("c", "clear_chat", "Clear"),
        ("r", "refresh_cursor", "Refresh"),
        ("i", "focus_input", "URL"),
        ("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: #0b0b0b;
        layers: base;
    }

    Header {
        background: #181818;
        color: #f1f1f1;
    }

    Footer {
        background: #181818;
        color: #9a9a9a;
    }

    /* ---------- top toolbar ---------- */
    #toolbar {
        height: 3;
        background: #212121;
        padding: 0 1;
    }

    #brand {
        width: 10;
        height: 3;
        content-align: left middle;
        color: #ffffff;
        text-style: bold;
    }

    #url-input {
        width: 1fr;
        height: 3;
        background: #121212;
        color: #f1f1f1;
        border: tall #303030;
        padding: 0 1;
    }

    #url-input:focus {
        border: tall #ff0000;
    }

    #status-chip {
        width: 22;
        height: 3;
        content-align: center middle;
        text-style: bold;
        background: #121212;
        color: #aaaaaa;
        border: tall #303030;
    }

    #status-chip.status-live {
        color: #ffffff;
        background: #ff0000;
        border: tall #ff0000;
    }

    #status-chip.status-connecting {
        color: #ffca28;
        border: tall #4a3a12;
    }

    #status-chip.status-error {
        color: #ffffff;
        background: #7f1d1d;
        border: tall #b91c1c;
    }

    #status-chip.status-ended {
        color: #cfcfcf;
        border: tall #3a3a3a;
    }

    /* ---------- body split ---------- */
    #body {
        height: 1fr;
    }

    /* ---------- LEFT: YouTube live chat ---------- */
    #chat-pane {
        width: 65%;
        background: #0f0f0f;
    }

    #chat-header {
        height: 1;
        background: #212121;
        color: #f1f1f1;
        padding: 0 1;
        text-style: bold;
    }

    #chat-log {
        height: 1fr;
        background: #0f0f0f;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-background: #0f0f0f;
        scrollbar-color: #383838;
        scrollbar-color-hover: #575757;
    }

    #chat-log:focus {
        border-left: tall #8b0000;
        padding: 0 1 0 0;
    }

    /* YouTube chat is dense: plain messages sit line to line, only the
       paid/membership cards get breathing room. */
    .chat-row {
        width: 1fr;
        height: auto;
        padding: 0 0;
    }

    .chat-row.superchat {
        background: #241a06;
        border-left: thick #ffca28;
        padding: 0 1;
        margin: 1 0;
    }

    .chat-row.supersticker {
        background: #05212a;
        border-left: thick #00b8d4;
        padding: 0 1;
        margin: 1 0;
    }

    .chat-row.newmember {
        background: #0c1f10;
        border-left: thick #2ba640;
        padding: 0 1;
        margin: 1 0;
    }

    .chat-notice {
        width: 1fr;
        height: auto;
        color: #6f6f6f;
        text-align: center;
        margin-bottom: 1;
    }

    /* ---------- RIGHT: Cursor sidebar ---------- */
    #cursor-pane {
        width: 35%;
        background: #1e1e1e;
        border-left: solid #2d2d30;
    }

    #cursor-header {
        height: 1;
        background: #252526;
        color: #cccccc;
        padding: 0 1;
        text-style: bold;
    }

    #cursor-body {
        height: 1fr;
        background: #1e1e1e;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-background: #1e1e1e;
        scrollbar-color: #3f3f46;
        scrollbar-color-hover: #5a5a62;
    }

    #cursor-body:focus {
        border-left: tall #0078d4;
        padding: 0 1 0 0;
    }

    .section-label {
        width: 1fr;
        height: 1;
        text-style: bold;
        margin-top: 1;
    }

    .agent-card {
        width: 1fr;
        height: auto;
        background: #252526;
        border-left: thick #3f3f46;
        padding: 0 1;
        margin-bottom: 1;
    }

    .agent-card.agent-running {
        background: #202b33;
        border-left: thick #0078d4;
    }

    .agent-card.agent-done {
        background: #232324;
        border-left: thick #3f3f46;
    }

    .shell-row {
        width: 1fr;
        height: auto;
        background: #1b1b1b;
        border-left: thick #2d2d30;
        padding: 0 1;
        margin-bottom: 1;
    }

    .empty-row {
        width: 1fr;
        height: 1;
        padding: 0 1;
    }

    #cursor-meta {
        height: auto;
        dock: bottom;
        background: #252526;
        color: #6f6f6f;
        padding: 0 1;
    }
    """

    def __init__(self, initial_target: str | None = None, publish: bool = True) -> None:
        super().__init__()
        self.initial_target = initial_target or ""
        self.publish_enabled = publish
        self.events: queue.Queue[ChatEvent] = queue.Queue(maxsize=4000)
        self.worker: ChatWorker | None = None
        self.paused = False
        self.message_count = 0
        self.status = "idle"
        self.status_note = ""
        self.video_id = ""
        self._last_signature: tuple = ()
        self._last_publish = 0.0

    # ------------------------------------------------------------------ UI

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="toolbar"):
            yield Static("[#ff0000]\u25b6[/] [b]yt-tui[/]", id="brand", markup=True)
            yield Input(
                placeholder="Paste a YouTube live URL or 11-char video id, then press Enter",
                id="url-input",
                value=self.initial_target,
            )
            yield Static("Idle", id="status-chip")
        with Horizontal(id="body"):
            with Vertical(id="chat-pane"):
                yield Static("", id="chat-header", markup=True)
                yield VerticalScroll(id="chat-log")
            with Vertical(id="cursor-pane"):
                yield Static(
                    "[b #cccccc]CURSOR ACTIVITY[/]", id="cursor-header", markup=True
                )
                yield VerticalScroll(id="cursor-body")
                yield Static("", id="cursor-meta", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._render_chat_header()
        self._set_status("idle")
        self.set_interval(0.1, self._drain_events)
        self.set_interval(CURSOR_REFRESH_SECONDS, self.action_refresh_cursor)
        self.action_refresh_cursor()

        if self.initial_target:
            self.connect_to(self.initial_target)
            self.query_one("#chat-log", VerticalScroll).focus()
        else:
            self.query_one("#url-input", Input).focus()
            self._notice("Paste a YouTube live URL above and press Enter.")

    # -------------------------------------------------------------- chat

    def connect_to(self, target: str) -> None:
        video_id = parse_video_id(target)
        if not video_id:
            self._set_status("error", "bad url")
            self._notice(f"Could not parse a video id from: {truncate(target, 60)}")
            return

        if self.worker is not None:
            self.worker.stop()
            self.worker = None

        self.video_id = video_id
        self.message_count = 0
        self._clear_rows()
        self._set_status("connecting")
        self._notice(f"Connecting to {video_id}\u2026")

        self.worker = ChatWorker(video_id, self.events)
        self.worker.start()

    async def _drain_events(self) -> None:
        if self.paused:
            return

        rows: list[ChatRow] = []
        status_changed = False
        for _ in range(120):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            if event.kind == "message" and event.message is not None:
                rows.append(ChatRow(event.message))
                self.message_count += 1
            elif event.kind == "status":
                self._set_status(event.text)
                status_changed = True
            elif event.kind == "error":
                self._set_status("error", event.text)
                self._notice(f"Error: {truncate(event.text, 90)}")
                status_changed = True
            elif event.kind == "ended":
                self._set_status("ended", event.text)
                self._notice("\u2014 stream ended \u2014")
                status_changed = True

        if rows:
            log = self.query_one("#chat-log", VerticalScroll)
            await log.mount_all(rows)
            await self._trim_rows()
            log.scroll_end(animate=True, duration=0.25)
            self._render_chat_header()
        elif status_changed:
            self._render_chat_header()

    async def _trim_rows(self) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        children = list(log.children)
        excess = len(children) - MAX_CHAT_ROWS
        if excess > 0:
            for widget in children[:excess]:
                await widget.remove()

    def _clear_rows(self) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        log.remove_children()

    def _notice(self, text: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        log.mount(Static(f"[{DIM}]{escape(text)}[/]", classes="chat-notice", markup=True))
        log.scroll_end(animate=False)

    def _render_chat_header(self) -> None:
        header = self.query_one("#chat-header", Static)
        if self.status == "live":
            pill = "[white on #ff0000] \u25cf LIVE [/]"
        elif self.status == "connecting":
            pill = "[black on #ffca28] \u2026 [/]"
        elif self.status == "error":
            pill = "[white on #7f1d1d] ERROR [/]"
        elif self.status == "ended":
            pill = f"[{DIM}] ENDED [/]"
        else:
            pill = f"[{DIM}] OFFLINE [/]"

        target = f"[{DIM}]{escape(self.video_id)}[/]" if self.video_id else ""
        paused = "  [black on #ffca28] PAUSED [/]" if self.paused else ""
        header.update(
            f"[b #f1f1f1]Live chat[/]  {pill}  "
            f"[{DIM}]{self.message_count} messages[/]  {target}{paused}"
        )

        if self.status == "live":
            self.query_one("#status-chip", Static).update(
                f"\u25cf LIVE  {self.message_count}"
            )

    def _set_status(self, status: str, note: str = "") -> None:
        self.status = status
        self.status_note = note
        chip = self.query_one("#status-chip", Static)
        chip.remove_class(
            "status-live", "status-connecting", "status-error", "status-ended"
        )
        if status == "live":
            chip.add_class("status-live")
            chip.update(f"\u25cf LIVE  {self.message_count}")
        elif status == "connecting":
            chip.add_class("status-connecting")
            chip.update("Connecting\u2026")
        elif status == "error":
            chip.add_class("status-error")
            chip.update("Error")
        elif status == "ended":
            chip.add_class("status-ended")
            chip.update("Ended")
        else:
            chip.update("Idle")
        self._render_chat_header()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "url-input":
            return
        value = event.value.strip()
        if value:
            self.connect_to(value)
            self.query_one("#chat-log", VerticalScroll).focus()

    # ------------------------------------------------------------ actions

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            log = self.query_one("#chat-log", VerticalScroll)
            log.mount(
                Static(f"[{DIM}]\u2014 paused \u2014[/]", classes="chat-notice", markup=True)
            )
            log.scroll_end(animate=False)
        self._render_chat_header()

    def action_clear_chat(self) -> None:
        self._clear_rows()
        self.message_count = 0
        self._render_chat_header()

    def action_focus_input(self) -> None:
        self.query_one("#url-input", Input).focus()

    def action_refresh_cursor(self) -> None:
        self.run_worker(
            self._load_snapshot,
            name="cursor-snapshot",
            group="cursor",
            exclusive=True,
            thread=True,
        )

    def _load_snapshot(self) -> Snapshot:
        """Runs on a worker thread: filesystem + network are blocking."""
        snapshot = build_snapshot()
        if self.publish_enabled:
            now = time.time()
            if now - self._last_publish >= PUBLISH_EVERY_SECONDS:
                self._last_publish = now
                try:
                    devices.publish(snapshot.local, snapshot.shells)
                except Exception:
                    pass
        return snapshot

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "cursor-snapshot":
            return
        result = event.worker.result
        if isinstance(result, Snapshot):
            self._render_cursor(result)

    # ------------------------------------------------------- cursor pane

    def _render_cursor(self, snapshot: Snapshot) -> None:
        signature = self._signature(snapshot)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        now = time.time()
        body = self.query_one("#cursor-body", VerticalScroll)
        # Cards sit inside a left border plus one column of padding each side.
        width = max(20, body.content_size.width - 3)
        widgets: list[Static] = []

        widgets.append(SectionLabel("THIS MAC"))
        if snapshot.local:
            widgets.extend(AgentCard(a, now, width) for a in snapshot.local[:6])
        else:
            widgets.append(EmptyRow("no local agents"))

        widgets.append(SectionLabel("OTHER DEVICES"))
        if snapshot.peers:
            for group in snapshot.peers:
                widgets.append(
                    Static(
                        f"[{CURSOR_ACCENT}]{escape(group.name)}[/] "
                        f"[{DIM}]{humanize_age(group.updated, now)}[/]",
                        classes="empty-row",
                        markup=True,
                    )
                )
                if group.agents:
                    widgets.extend(AgentCard(a, now, width) for a in group.agents[:4])
                else:
                    widgets.append(EmptyRow("  idle"))
        else:
            widgets.append(EmptyRow("none"))

        widgets.append(SectionLabel("CLOUD"))
        if snapshot.cloud:
            widgets.extend(AgentCard(a, now, width) for a in snapshot.cloud[:5])
        else:
            widgets.append(EmptyRow("none"))

        widgets.append(SectionLabel("SHELLS"))
        if snapshot.shells:
            widgets.extend(ShellRow(s, now, width) for s in snapshot.shells[:5])
        else:
            widgets.append(EmptyRow("none"))

        body.remove_children()
        body.mount_all(widgets)

        meta = self.query_one("#cursor-meta", Static)
        counts = f"{snapshot.running_count} running \u00b7 {snapshot.done_count} done"
        if snapshot.cloud_note:
            counts = f"{counts} \u00b7 cloud: {snapshot.cloud_note}"
        sync = _shorten_path(snapshot.sync_path, width - 6)
        meta.update(
            f"[{DIM}]{escape(truncate(counts, width + 1))}\n"
            f"sync: {escape(sync) if sync else '(none)'}[/]"
        )

    @staticmethod
    def _signature(snapshot: Snapshot) -> tuple:
        """Cheap change detector so we only re-mount when something moved."""

        def agent_key(agent: AgentInfo) -> tuple:
            return (
                agent.agent_id,
                agent.status,
                agent.detail,
                round(agent.updated),
                len(agent.todos),
                tuple(t.status for t in agent.todos),
            )

        return (
            tuple(agent_key(a) for a in snapshot.local),
            tuple(
                (g.name, round(g.updated), tuple(agent_key(a) for a in g.agents))
                for g in snapshot.peers
            ),
            tuple(agent_key(a) for a in snapshot.cloud),
            tuple((s.command, s.status, round(s.updated)) for s in snapshot.shells),
            snapshot.cloud_note,
        )

    # -------------------------------------------------------------- exit

    def on_resize(self) -> None:
        # Cards are laid out for a measured width, so force a rebuild.
        self._last_signature = ()

    def on_unmount(self) -> None:
        if self.worker is not None:
            self.worker.stop()


def _shorten_path(path: str, limit: int = 46) -> str:
    if not path:
        return ""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home):
        text = "~" + text[len(home) :]
    return truncate(text, max(12, limit))


def run(target: str | None = None, publish: bool = True) -> None:
    YtTuiApp(initial_target=target, publish=publish).run()
