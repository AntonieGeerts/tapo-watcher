"""Settings, read from .env in the project root (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def unset_placeholder(value: str | None) -> str:
    """"your-..." values are the unfilled placeholders from .env.example."""
    value = (value or "").strip()
    return "" if value.startswith("your-") else value


def _env(name: str, default: str = "") -> str:
    return unset_placeholder(os.environ.get(name, default))


def _path(name: str, default: str) -> Path:
    path = Path(_env(name, default)).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass
class Settings:
    camera_host: str
    camera_user: str
    camera_pass: str
    onvif_port: int
    rtsp_port: int
    rtsp_stream: str
    live_stream: str
    rtsp_url_override: str
    trigger_mode: str
    snapshots: int
    snapshot_interval: float
    cooldown_seconds: int
    keep_days: int
    decisions_key_file: Path
    decisions_model: str
    person_min_prob: float
    verdict_min_prob: float
    events_dir: Path
    web_host: str
    web_port: int

    @property
    def camera_configured(self) -> bool:
        return bool(self.camera_host and self.camera_user and self.camera_pass)

    @property
    def can_stream(self) -> bool:
        return self.camera_configured or bool(self.rtsp_url_override)

    def rtsp_url(self, stream: str | None = None) -> str:
        if self.rtsp_url_override:
            return self.rtsp_url_override
        auth = f"{quote(self.camera_user, safe='')}:{quote(self.camera_pass, safe='')}@"
        return f"rtsp://{auth}{self.camera_host}:{self.rtsp_port}/{stream or self.rtsp_stream}"

    def reload_credentials(self) -> None:
        """Pick up camera settings added to .env while the server is running."""
        values = dotenv_values(ROOT / ".env")
        self.camera_host = unset_placeholder(values.get("CAMERA_HOST")) or self.camera_host
        self.camera_user = unset_placeholder(values.get("CAMERA_USER")) or self.camera_user
        self.camera_pass = unset_placeholder(values.get("CAMERA_PASS"))

    def redact(self, text: str) -> str:
        """Strip the camera password from text that may echo the RTSP URL (ffmpeg errors)."""
        for secret in {self.camera_pass, quote(self.camera_pass, safe="")}:
            if secret:
                text = text.replace(secret, "***")
        return text


def load_settings() -> Settings:
    trigger_mode = _env("TRIGGER_MODE", "motion").lower()
    if trigger_mode not in ("auto", "person", "motion"):
        raise SystemExit(f"TRIGGER_MODE must be auto, person or motion, not {trigger_mode!r}")
    return Settings(
        camera_host=_env("CAMERA_HOST"),
        camera_user=_env("CAMERA_USER"),
        camera_pass=_env("CAMERA_PASS"),
        onvif_port=int(_env("ONVIF_PORT", "2020")),
        rtsp_port=int(_env("RTSP_PORT", "554")),
        rtsp_stream=_env("RTSP_STREAM", "stream1"),
        live_stream=_env("LIVE_STREAM", "stream2"),
        rtsp_url_override=_env("RTSP_URL"),
        trigger_mode=trigger_mode,
        snapshots=max(1, int(_env("SNAPSHOTS", "3"))),
        snapshot_interval=max(0.2, float(_env("SNAPSHOT_INTERVAL", "1.5"))),
        cooldown_seconds=int(_env("COOLDOWN_SECONDS", "20")),
        keep_days=int(_env("KEEP_DAYS", "14")),
        decisions_key_file=_path("DECISIONS_KEY_FILE", "decisionsapi.txt"),
        decisions_model=_env("DECISIONS_MODEL", "gpt-6-luna"),
        person_min_prob=float(_env("PERSON_MIN_PROB", "0.5")),
        verdict_min_prob=float(_env("VERDICT_MIN_PROB", "0.6")),
        events_dir=_path("EVENTS_DIR", "events"),
        web_host=_env("WEB_HOST", "127.0.0.1"),
        web_port=int(_env("WEB_PORT", "8765")),
    )
