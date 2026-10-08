"""Re-run the Decisions API on saved events, e.g. after changing the question.

    .venv/bin/python -m app.reanalyze               # every event
    .venv/bin/python -m app.reanalyze <event-id>...  # just these
"""
from __future__ import annotations

import asyncio
import sys

from .config import load_settings
from .decisions import DecisionsClient, read_api_key, summarize
from .store import EventStore


async def main(event_ids: list[str]) -> None:
    settings = load_settings()
    store = EventStore(settings.events_dir)
    key = read_api_key(settings.decisions_key_file)
    if not key:
        raise SystemExit(f"No API key in {settings.decisions_key_file}")
    client = DecisionsClient(key, settings.decisions_model)
    events = [store.load(i) for i in event_ids] if event_ids else store.list(limit=100_000)
    for event in filter(None, events):
        frames = sorted(store.folder(event["id"]).glob("frame_*.jpg"))
        if not frames:
            continue
        results = await asyncio.gather(*(client.ask_frame(f) for f in frames))
        analysis = summarize(results, settings.person_min_prob, settings.verdict_min_prob)
        event.update(analysis=analysis, status="done", thumbnail=analysis.get("best_frame") or frames[0].name)
        store.save(event)
        print(event["id"], analysis["label"], analysis.get("probability", ""))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
