"""Realtime peer sync over MQTT.

GitHub gist is too slow/rate-limited for live updates. Both Macs publish
agent snapshots to a private MQTT topic derived from the shared gist id
and apply inbound messages into an in-memory peer cache instantly.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Callable

_DEFAULT_HOST = "broker.hivemq.com"
_DEFAULT_PORT = 1883

_bus_lock = threading.Lock()
_bus: "RealtimeBus | None" = None


def get_bus(channel: str) -> "RealtimeBus":
    """Process-wide bus keyed by channel (gist id)."""
    global _bus
    with _bus_lock:
        if _bus is not None and _bus.channel == channel:
            return _bus
        if _bus is not None:
            _bus.close()
        _bus = RealtimeBus(channel)
        return _bus


class RealtimeBus:
    """Pub/sub for device payloads. Thread-safe peer cache."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.peers: dict[str, dict[str, Any]] = {}
        self.connected = False
        self.last_error = ""
        self._self_id = ""
        self._lock = threading.RLock()
        self._client: Any = None
        self._on_peer: Callable[[], None] | None = None
        self._host = os.environ.get("YT_TUI_MQTT_HOST", _DEFAULT_HOST).strip() or _DEFAULT_HOST
        try:
            self._port = int(os.environ.get("YT_TUI_MQTT_PORT", str(_DEFAULT_PORT)))
        except ValueError:
            self._port = _DEFAULT_PORT
        self._start()

    def set_self(self, machine_id: str) -> None:
        self._self_id = machine_id

    def on_peer_update(self, callback: Callable[[], None] | None) -> None:
        self._on_peer = callback

    def _topic_root(self) -> str:
        return f"yt-tui/{self.channel}"

    def _start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            self.last_error = f"paho-mqtt missing: {exc}"
            return

        client_id = f"yt-tui-{uuid.uuid4().hex[:12]}"
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
            version2 = True
        except Exception:
            client = mqtt.Client(client_id=client_id)
            version2 = False

        def _connected(reason: Any) -> None:
            code = getattr(reason, "value", reason)
            if isinstance(code, int) and code != 0:
                self.connected = False
                self.last_error = f"mqtt connect rc={code}"
                return
            if isinstance(code, str) and code not in ("Success", "success", ""):
                # Some paho builds use ReasonCode string forms.
                if "Success" not in str(code):
                    self.connected = False
                    self.last_error = f"mqtt connect rc={code}"
                    return
            self.connected = True
            self.last_error = ""
            client.subscribe(f"{self._topic_root()}/+", qos=0)

        if version2:

            def on_connect(
                client: Any,
                userdata: Any,
                flags: Any,
                reason_code: Any,
                properties: Any = None,
            ) -> None:
                _connected(reason_code)

            def on_disconnect(
                client: Any,
                userdata: Any,
                flags: Any,
                reason_code: Any,
                properties: Any = None,
            ) -> None:
                self.connected = False
        else:

            def on_connect(client: Any, userdata: Any, flags: Any, rc: Any) -> None:  # type: ignore[misc]
                _connected(rc)

            def on_disconnect(client: Any, userdata: Any, rc: Any) -> None:  # type: ignore[misc]
                self.connected = False

        def on_message(client: Any, userdata: Any, msg: Any) -> None:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(payload, dict):
                return
            mid = str(payload.get("machine_id") or "")
            if not mid or mid == self._self_id:
                return
            with self._lock:
                self.peers[mid] = payload
            callback = self._on_peer
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        self._client = client

        try:
            client.connect(self._host, self._port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            self.last_error = f"mqtt connect failed: {exc}"
            self._client = None
            return

        # Wait briefly for CONNACK so the first publish is not dropped.
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.connected:
            time.sleep(0.05)

    def wait_connected(self, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.connected:
                return True
            time.sleep(0.05)
        return self.connected

    def publish(self, machine_id: str, payload: dict[str, Any]) -> bool:
        """Push this machine's snapshot to peers. Non-blocking once connected."""
        client = self._client
        if client is None:
            return False
        if not self.connected and not self.wait_connected(1.5):
            self.last_error = self.last_error or "mqtt not connected"
            return False

        self._self_id = machine_id
        body = dict(payload)
        body["machine_id"] = machine_id
        body["transport"] = "mqtt"
        try:
            info = client.publish(
                f"{self._topic_root()}/{machine_id}",
                json.dumps(body, ensure_ascii=False),
                qos=0,
                retain=True,
            )
            rc = getattr(info, "rc", 0)
            ok = rc == 0
            if not ok:
                self.last_error = f"mqtt publish rc={rc}"
            return ok
        except Exception as exc:
            self.last_error = f"mqtt publish failed: {exc}"
            return False

    def peer_payloads(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.peers.values())

    def status_line(self) -> str:
        with self._lock:
            n = len(self.peers)
        state = "live" if self.connected else "connecting"
        err = f" ({self.last_error})" if self.last_error else ""
        return f"realtime   : mqtt {state}, {n} peer cache{err}"

    def close(self) -> None:
        client = self._client
        self._client = None
        self.connected = False
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
