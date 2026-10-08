"""Getting video out of the camera with ffmpeg."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from .config import Settings


class CaptureError(Exception):
    pass


def _input_args(url: str) -> list[str]:
    if url.startswith("rtsp://"):
        return ["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url]
    # A local video file (RTSP_URL override, used for testing), played back in real time.
    return ["-re", "-stream_loop", "-1", "-i", url]


async def capture_snapshots(settings: Settings, folder: Path) -> list[Path]:
    """Save SNAPSHOTS pictures from the camera, SNAPSHOT_INTERVAL seconds apart; return their paths."""
    fps = f"{1 / settings.snapshot_interval:.4f}"
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *_input_args(settings.rtsp_url()),
        "-map", "0:v:0", "-vf", f"fps={fps},scale='min(1280,iw)':-2", "-frames:v", str(settings.snapshots),
        "-q:v", "3", str(folder / "frame_%02d.jpg"),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        timeout = settings.snapshots * settings.snapshot_interval + 30
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise CaptureError("ffmpeg timed out reading the camera stream") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    frames = sorted(folder.glob("frame_*.jpg"))
    if proc.returncode != 0 or not frames:
        lines = stderr.decode(errors="replace").strip().splitlines()
        detail = lines[-1] if lines else f"exit code {proc.returncode}"
        raise CaptureError(f"ffmpeg could not get pictures from the camera: {settings.redact(detail)}")
    return frames


async def mjpeg(settings: Settings) -> AsyncIterator[bytes]:
    """Live view as multipart MJPEG; ffmpeg runs only while someone is watching."""
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        *_input_args(settings.rtsp_url(settings.live_stream)),
        "-an", "-vf", "fps=5,scale='min(960,iw)':-2", "-q:v", "5", "-f", "mjpeg", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    buffer = b""
    try:
        while chunk := await proc.stdout.read(65536):
            buffer += chunk
            while (start := buffer.find(b"\xff\xd8")) >= 0 and (end := buffer.find(b"\xff\xd9", start + 2)) >= 0:
                frame, buffer = buffer[start : end + 2], buffer[end + 2 :]
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n" + frame + b"\r\n"
                )
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
