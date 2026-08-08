"""Background YouTube live chat worker.

pytchat installs signal handlers when `interruptable=True`, which raises
"signal only works in main thread" off the main thread. We always pass
`interruptable=False` and drive the loop ourselves.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .emoji_text import compose_chat_text
from .utils import author_color

MSG_TEXT = "textMessage"
MSG_SUPERCHAT = "superChat"
MSG_STICKER = "superSticker"
MSG_MEMBER = "newSponsor"


@dataclass(slots=True)
class ChatMessage:
    """UI-safe snapshot of a chat item (never holds a pytchat object)."""

    msg_id: str
    kind: str
    author: str
    color: str
    timestamp: str
    text: str
    amount: str = ""
    currency: str = ""
    is_owner: bool = False
    is_moderator: bool = False
    is_member: bool = False
    is_verified: bool = False

    @property
    def badges(self) -> list[tuple[str, str]]:
        """(label, css_class) pairs, highest authority first."""
        out: list[tuple[str, str]] = []
        if self.is_owner:
            out.append(("OWNER", "badge-owner"))
        if self.is_moderator:
            out.append(("MOD", "badge-mod"))
        if self.is_member:
            out.append(("MEMBER", "badge-member"))
        if self.is_verified:
            out.append(("VERIFIED", "badge-verified"))
        return out


@dataclass(slots=True)
class ChatEvent:
    """Anything the worker wants to tell the UI."""

    kind: str  # status | message | error | ended
    message: ChatMessage | None = None
    text: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _hhmmss(value: str) -> str:
    """'2026-08-08 05:04:11' -> '05:04'."""
    text = (value or "").strip()
    if " " in text:
        clock = text.split(" ", 1)[1]
        return clock[:5] if len(clock) >= 5 else clock
    return time.strftime("%H:%M")


def _to_message(item: Any) -> ChatMessage:
    author = getattr(item, "author", None)
    name = getattr(author, "name", "") or "unknown"
    channel_id = getattr(author, "channelId", "") or ""
    kind = getattr(item, "type", MSG_TEXT) or MSG_TEXT
    text = compose_chat_text(item)

    if kind == MSG_STICKER and not text:
        text = "sent a Super Sticker"
    if kind == MSG_MEMBER and not text:
        text = "became a new member"

    return ChatMessage(
        msg_id=str(getattr(item, "id", "") or id(item)),
        kind=kind,
        author=name,
        color=author_color(name, channel_id),
        timestamp=_hhmmss(str(getattr(item, "datetime", ""))),
        text=text,
        amount=str(getattr(item, "amountString", "") or ""),
        currency=str(getattr(item, "currency", "") or ""),
        is_owner=bool(getattr(author, "isChatOwner", False)),
        is_moderator=bool(getattr(author, "isChatModerator", False)),
        is_member=bool(getattr(author, "isChatSponsor", False)),
        is_verified=bool(getattr(author, "isVerified", False)),
    )


class ChatWorker:
    """Polls a YouTube live chat on a daemon thread and queues events."""

    def __init__(self, video_id: str, events: queue.Queue[ChatEvent]) -> None:
        self.video_id = video_id
        self.events = events
        self._stop = threading.Event()
        self._chat: Any = None
        self._thread = threading.Thread(
            target=self._run, name=f"chat-{video_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        chat = self._chat
        if chat is not None:
            try:
                chat.terminate()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _emit(self, kind: str, text: str = "", message: ChatMessage | None = None) -> None:
        try:
            self.events.put_nowait(ChatEvent(kind=kind, text=text, message=message))
        except queue.Full:
            pass

    def _run(self) -> None:
        try:
            import pytchat
        except Exception as exc:
            self._emit("error", f"pytchat unavailable: {exc}")
            return

        self._emit("status", "connecting")

        try:
            # interruptable=False is required: signal handlers only work on
            # the main thread.
            self._chat = pytchat.create(
                video_id=self.video_id,
                interruptable=False,
                hold_exception=True,
            )
        except Exception as exc:
            self._emit("error", f"{type(exc).__name__}: {exc}")
            return

        self._emit("status", "live")
        ended = False
        try:
            while not self._stop.is_set() and self._chat.is_alive():
                try:
                    data = self._chat.get()
                except TypeError:
                    # pytchat dereferences a None chat component once the
                    # continuation token runs out, i.e. the chat is over.
                    ended = True
                    break
                except Exception as exc:
                    self._emit("error", f"{type(exc).__name__}: {exc}")
                    return

                # get() returns a bare [] once the chat is no longer alive.
                if not hasattr(data, "sync_items"):
                    ended = True
                    break

                for item in data.sync_items():
                    if self._stop.is_set():
                        break
                    try:
                        self._emit("message", message=_to_message(item))
                    except Exception:
                        continue

                if not getattr(data, "items", None):
                    time.sleep(0.4)
        finally:
            if not self._stop.is_set():
                failure: Exception | None = None
                try:
                    self._chat.raise_for_status()
                except Exception as exc:
                    failure = exc
                if failure is not None and not isinstance(failure, TypeError):
                    self._emit("error", f"{type(failure).__name__}: {failure}")
                else:
                    self._emit("ended", "stream ended" if ended or failure else "chat closed")
