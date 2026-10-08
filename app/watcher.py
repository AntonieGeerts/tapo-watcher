"""Follows the camera's ONVIF events and fires a callback when a person (or motion) appears."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from .config import Settings
from .onvif import Notification, OnvifClient, OnvifError, Subscription

log = logging.getLogger("tapo.watcher")

RENEW_EVERY = 240  # seconds; subscriptions are requested for 10 minutes


def event_kind(text: str) -> str | None:
    """'person' or 'motion' for a topic (plus its data item names), else None."""
    lowered = text.lower()
    if any(word in lowered for word in ("people", "person", "human")):
        return "person"
    if "motion" in lowered:
        return "motion"
    return None


def _flag(data: dict[str, str]) -> bool | None:
    for value in data.values():
        value = (value or "").strip().lower()
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
    return None


class EventWatcher:
    def __init__(self, settings: Settings, on_trigger: Callable[[str, str], object]):
        self.settings = settings
        self.on_trigger = on_trigger
        self.state = "starting"
        self.error: str | None = None
        self.topics: list[str] = []
        self.trigger_on: str | None = None
        self.recent: deque[dict] = deque(maxlen=40)
        self._active: dict[str, bool] = {}
        self._retry_delay = 5

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "trigger_on": self.trigger_on,
            "topics": self.topics,
            "recent": list(self.recent),
        }

    async def run(self) -> None:
        while not self.settings.camera_configured:
            self.state = "needs_config"
            self.error = (
                "Waiting for CAMERA_HOST, CAMERA_USER and CAMERA_PASS in .env (the Camera Account "
                "from the Tapo app). They are picked up automatically, no restart needed."
            )
            await asyncio.sleep(3)
            self.settings.reload_credentials()
        log.info("Camera credentials found; connecting to %s", self.settings.camera_host)
        while True:
            try:
                await self._follow()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep retrying whatever goes wrong
                message = str(e) or e.__class__.__name__
                if "NotAuthorized" in message:
                    message += " (check CAMERA_USER / CAMERA_PASS: use the Camera Account from the Tapo app)"
                self.state, self.error = "error", message
                log.warning("ONVIF: %s; retrying in %ss", message, self._retry_delay)
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, 120)

    async def _follow(self) -> None:
        s = self.settings
        client = OnvifClient(s.camera_host, s.onvif_port, s.camera_user, s.camera_pass)
        sub: Subscription | None = None
        try:
            self.state = "connecting"
            await client.sync_clock()
            events_url = await client.events_url()
            try:
                self.topics = await client.event_topics(events_url)
            except OnvifError as e:
                log.info("Camera did not list its event topics (%s); continuing", e)
            if s.trigger_mode == "auto":
                advertised = any(event_kind(t) == "person" for t in self.topics)
                self.trigger_on = "person" if advertised else "motion"
            else:
                self.trigger_on = s.trigger_mode

            sub = await client.subscribe(events_url)
            self.state, self.error, self._retry_delay = "connected", None, 5
            log.info("Following camera events; capturing on %s", self.trigger_on)
            renewed = time.monotonic()
            while True:
                started = time.monotonic()
                notifications = await client.pull(sub)
                for note in notifications:
                    self._handle(note)
                if time.monotonic() - renewed > RENEW_EVERY:
                    try:
                        await client.renew(sub)
                    except OnvifError:
                        # Some Tapo firmware rejects Renew; a fresh subscription works just as well.
                        with contextlib.suppress(Exception):
                            await client.unsubscribe(sub)
                        sub = await client.subscribe(events_url)
                    renewed = time.monotonic()
                if not notifications and time.monotonic() - started < 1:
                    await asyncio.sleep(1)  # firmware that answers PullMessages without waiting
        finally:
            if sub is not None:
                with contextlib.suppress(Exception):
                    await client.unsubscribe(sub)
            await client.aclose()

    def _handle(self, note: Notification) -> None:
        topic = note.topic.split(":", 1)[-1]  # drop the "tns1:" prefix
        now = datetime.now().strftime("%H:%M:%S")
        entry = {"topic": topic, "operation": note.operation, "data": note.data}
        if self.recent and all(self.recent[0][k] == v for k, v in entry.items()):
            # Tapo repeats the same message several times a second while motion lasts.
            self.recent[0].update(time=now, count=self.recent[0]["count"] + 1)
        else:
            self.recent.appendleft({"time": now, **entry, "count": 1})
        flag = _flag(note.data)
        if flag is None:
            return
        kind = event_kind(" ".join([topic, *note.data]))
        if kind == "person" and self.settings.trigger_mode == "auto" and self.trigger_on != "person":
            log.info("Camera sends person events after all; capturing on person from now on")
            self.trigger_on = "person"

        # Only the value counts, not PropertyOperation: Tapo sends every update as
        # "Initialized", not just the state at subscription time.
        was_active = self._active.get(note.topic, False)
        self._active[note.topic] = flag
        if flag and not was_active and kind == self.trigger_on:
            self.on_trigger(kind, topic)
