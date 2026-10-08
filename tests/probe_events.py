"""Probe how the real camera answers PullMessages (uses the credentials in .env).

    .venv/bin/python -m tests.probe_events
"""
from __future__ import annotations

import asyncio
import time

from app.config import load_settings
from app.onvif import EVENTS_WSDL, OnvifClient, _parse_notification, NS


async def attempt(client: OnvifClient, sub, label: str, wait: int, *, auth: bool, addressing: bool) -> None:
    body = (
        f'<PullMessages xmlns="{EVENTS_WSDL}">'
        f"<Timeout>PT{wait}S</Timeout><MessageLimit>50</MessageLimit></PullMessages>"
    )
    started = time.monotonic()
    try:
        result = await client._call(
            sub.address,
            f"{EVENTS_WSDL}/PullPointSubscription/PullMessagesRequest",
            body,
            auth=auth,
            addressing=addressing,
            extra_headers=sub.reference_headers if addressing else "",
            timeout=wait + 15,
        )
        notes = [_parse_notification(n) for n in result.iterfind(".//wsnt:NotificationMessage", NS)]
        summary = [(n.topic.rsplit("/", 1)[-1], n.operation, n.data) for n in notes]
        print(f"{label:<28} OK after {time.monotonic() - started:4.1f}s, {len(notes)} messages {summary}")
    except Exception as e:
        cause = f" ({e.__cause__!r})" if e.__cause__ else ""
        print(f"{label:<28} FAILED after {time.monotonic() - started:4.1f}s: {e}{cause}")


async def main() -> None:
    s = load_settings()
    client = OnvifClient(s.camera_host, s.onvif_port, s.camera_user, s.camera_pass)
    await client.sync_clock()
    print(f"camera clock offset: {client.clock_offset:+.1f}s")
    url = await client.events_url()
    sub = await client.subscribe(url)
    print(f"subscription: {sub.address} (reference params: {sub.reference_headers or 'none'})")
    variants = [
        ("auth+addressing, wait 2s", 2, True, True),
        ("auth only, wait 2s", 2, True, False),
        ("addressing only, wait 2s", 2, False, True),
        ("neither, wait 2s", 2, False, False),
        ("auth+addressing, wait 8s", 8, True, True),
    ]
    for label, wait, auth, addressing in variants:
        await attempt(client, sub, label, wait, auth=auth, addressing=addressing)
    try:
        await client.unsubscribe(sub)
    except Exception as e:
        print(f"unsubscribe failed: {e}")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
