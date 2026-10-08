"""Tapo person watcher.

ONVIF motion event -> grab a few pictures with ffmpeg -> ask the OpenAI Decisions API whether
anyone in them is using a mobile phone -> show it all on a small dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .capture import capture_snapshots, mjpeg
from .config import load_settings
from .decisions import DecisionsClient, read_api_key, summarize
from .store import EventStore
from .watcher import EventWatcher

log = logging.getLogger("tapo")
settings = load_settings()
store = EventStore(settings.events_dir)
STATIC = Path(__file__).parent / "static"


class Pipeline:
    """One capture + analysis at a time, with a cooldown between camera-triggered events."""

    def __init__(self) -> None:
        key = read_api_key(settings.decisions_key_file)
        self.decisions = DecisionsClient(key, settings.decisions_model) if key else None
        self.busy = False
        self.last_start = float("-inf")
        self.tasks: set[asyncio.Task] = set()

    def trigger(self, kind: str, topic: str, *, manual: bool = False) -> str | None:
        now = time.monotonic()
        if self.busy or (not manual and now - self.last_start < settings.cooldown_seconds):
            log.info("Ignoring %s event (%s)", kind, "already capturing" if self.busy else "cooldown")
            return None
        self.busy, self.last_start = True, now
        event_id, folder = store.create()
        event = {
            "id": event_id,
            "time": datetime.now().isoformat(timespec="seconds"),
            "trigger": kind,
            "topic": topic,
            "status": "capturing",
        }
        store.save(event)
        log.info("Event %s: %s, taking %d pictures", event_id, kind, settings.snapshots)
        task = asyncio.create_task(self._process(event, folder))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return event_id

    async def _process(self, event: dict, folder: Path) -> None:
        try:
            frames = await capture_snapshots(settings, folder)
            event.update(frames=[f.name for f in frames], thumbnail=frames[0].name)
            if self.decisions is None:
                event["status"] = "done"
                return
            event["status"] = "analyzing"
            store.save(event)
            results = await asyncio.gather(*(self.decisions.ask_frame(f) for f in frames))
            analysis = summarize(results, settings.person_min_prob, settings.verdict_min_prob)
            event.update(analysis=analysis, status="done", thumbnail=analysis.get("best_frame") or frames[0].name)
            log.info("Event %s: %s", event["id"], analysis["label"])
        except Exception as e:
            event.update(status="error", error=settings.redact(str(e)) or e.__class__.__name__)
            log.warning("Event %s failed: %s", event["id"], event["error"])
        finally:
            store.save(event)
            self.busy = False


pipeline = Pipeline()
watcher = EventWatcher(settings, pipeline.trigger)


async def prune_forever() -> None:
    while True:
        if settings.keep_days > 0 and (removed := store.prune(settings.keep_days)):
            log.info("Removed %d events older than %d days", removed, settings.keep_days)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.mark_interrupted()
    if pipeline.decisions is None:
        log.warning("No API key in %s: events are recorded but not analysed", settings.decisions_key_file)
    background = [asyncio.create_task(watcher.run()), asyncio.create_task(prune_forever())]
    yield
    tasks = [*background, *pipeline.tasks]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Tapo watcher", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.events_dir), name="media")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status() -> dict:
    return {
        "camera": watcher.status(),
        "busy": pipeline.busy,
        "analysis_enabled": pipeline.decisions is not None,
        "settings": {
            "camera_host": settings.camera_host,
            "trigger_mode": settings.trigger_mode,
            "snapshots": settings.snapshots,
            "cooldown_seconds": settings.cooldown_seconds,
            "model": settings.decisions_model,
        },
    }


@app.get("/api/events")
def list_events() -> list[dict]:
    return store.list()


@app.post("/api/events", status_code=201)
def capture_now() -> dict:
    if not settings.can_stream:
        raise HTTPException(400, "Set CAMERA_HOST, CAMERA_USER and CAMERA_PASS in .env first")
    event_id = pipeline.trigger("manual", "manual", manual=True)
    if event_id is None:
        raise HTTPException(409, "Already capturing an event")
    return {"id": event_id}


@app.delete("/api/events/{event_id}", status_code=204)
def delete_event(event_id: str) -> None:
    if not store.delete(event_id):
        raise HTTPException(404, "No such event")


@app.get("/live.mjpg", include_in_schema=False)
def live() -> StreamingResponse:
    if not settings.can_stream:
        raise HTTPException(400, "Set CAMERA_HOST, CAMERA_USER and CAMERA_PASS in .env first")
    return StreamingResponse(mjpeg(settings), media_type="multipart/x-mixed-replace; boundary=frame")
