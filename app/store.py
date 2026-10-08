"""Events live on disk as events/<id>/{event.json, frame_NN.jpg}."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class EventStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def create(self) -> tuple[str, Path]:
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        event_id, n = base, 1
        while (self.root / event_id).exists():
            n += 1
            event_id = f"{base}-{n}"
        folder = self.root / event_id
        folder.mkdir()
        return event_id, folder

    def folder(self, event_id: str) -> Path | None:
        if not ID_RE.match(event_id):
            return None
        folder = self.root / event_id
        return folder if folder.is_dir() else None

    def save(self, event: dict) -> None:
        path = self.root / event["id"] / "event.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(event, indent=2))
        tmp.replace(path)

    def load(self, event_id: str) -> dict | None:
        folder = self.folder(event_id)
        if folder is None:
            return None
        try:
            return json.loads((folder / "event.json").read_text())
        except (OSError, ValueError):
            return None

    def list(self, limit: int = 300) -> list[dict]:
        events = []
        for folder in sorted(self.root.iterdir(), reverse=True):
            if len(events) >= limit:
                break
            if event := self.load(folder.name):
                events.append(event)
        return events

    def delete(self, event_id: str) -> bool:
        folder = self.folder(event_id)
        if folder is None:
            return False
        shutil.rmtree(folder)
        return True

    def prune(self, keep_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=keep_days)
        removed = 0
        for folder in self.root.iterdir():
            if ID_RE.match(folder.name) and datetime.strptime(folder.name[:15], "%Y%m%d-%H%M%S") < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed

    def mark_interrupted(self) -> None:
        """Events still capturing or analysing when the server stopped will never finish."""
        for event in self.list(limit=50):
            if event.get("status") in ("capturing", "analyzing"):
                event.update(status="error", error="interrupted by a server restart")
                self.save(event)
