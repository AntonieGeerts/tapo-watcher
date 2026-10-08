"""A fake Tapo ONVIF service, for testing the watcher without the camera.

Listens on 127.0.0.1:18080 and accepts user "mock" / password "mockpass", checking the
WS-Security digest like the real camera does. Every new subscription gets a motion event
after 3 seconds and a person event a second later, both cleared a few seconds after. Like a
real Tapo, every message is marked "Initialized" and motion repeats while it lasts.

    .venv/bin/python -m tests.mock_camera
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, Response

USER, PASSWORD = "mock", "mockpass"
BASE = "http://127.0.0.1:18080"
NAMESPACES = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://www.w3.org/2005/08/addressing" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
    'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" xmlns:tns1="http://www.onvif.org/ver10/topics"'
)
TOPICS = (
    "<tev:GetEventPropertiesResponse><wstop:TopicSet><tns1:RuleEngine>"
    '<CellMotionDetector><Motion wstop:topic="true"><tt:MessageDescription IsProperty="true">'
    '<tt:Data><tt:SimpleItemDescription Name="IsMotion" Type="xs:boolean"/></tt:Data>'
    "</tt:MessageDescription></Motion></CellMotionDetector>"
    '<PeopleDetector><People wstop:topic="true"><tt:MessageDescription IsProperty="true">'
    '<tt:Data><tt:SimpleItemDescription Name="IsPeople" Type="xs:boolean"/></tt:Data>'
    "</tt:MessageDescription></People></PeopleDetector>"
    "</tns1:RuleEngine></wstop:TopicSet></tev:GetEventPropertiesResponse>"
)
EVENT_TYPES = {
    "motion": ("tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion"),
    "person": ("tns1:RuleEngine/PeopleDetector/People", "IsPeople"),
}

app = FastAPI()
queues: dict[str, list[tuple[float, str, str, bool]]] = {}


def soap(body: str, status: int = 200) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NAMESPACES}><s:Body>{body}</s:Body></s:Envelope>'
    return Response(xml, status_code=status, media_type="application/soap+xml")


def fault(reason: str, code: str = "ter:ActionNotSupported") -> Response:
    return soap(
        f"<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>{code}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason></s:Fault>',
        400,
    )


def authorized(xml: str) -> bool:
    def field(tag: str) -> str | None:
        match = re.search(rf"<(?:\w+:)?{tag}\b[^>]*>([^<]*)</", xml)
        return match.group(1) if match else None

    user, digest, nonce, created = (field(t) for t in ("Username", "Password", "Nonce", "Created"))
    if not (user and digest and nonce and created):
        return False
    expected = hashlib.sha1(base64.b64decode(nonce) + created.encode() + PASSWORD.encode()).digest()
    return user == USER and digest == base64.b64encode(expected).decode()


def notification(operation: str, kind: str, active: bool) -> str:
    topic, item = EVENT_TYPES[kind]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<wsnt:NotificationMessage><wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
        f"{topic}</wsnt:Topic><wsnt:Message>"
        f'<tt:Message UtcTime="{now}" PropertyOperation="{operation}">'
        '<tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsconf"/></tt:Source>'
        f'<tt:Data><tt:SimpleItem Name="{item}" Value="{str(active).lower()}"/></tt:Data>'
        "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
    )


@app.post("/onvif/{path:path}")
async def handle(path: str, request: Request) -> Response:
    xml = (await request.body()).decode()
    if "GetSystemDateAndTime" in xml:
        n = datetime.now(timezone.utc)
        return soap(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
            f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )
    if not authorized(xml):
        return fault("Sender not Authorized", "ter:NotAuthorized")
    if "GetCapabilities" in xml:
        return soap(
            "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Events>"
            f"<tt:XAddr>{BASE}/onvif/service</tt:XAddr><tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "</tt:Events></tds:Capabilities></tds:GetCapabilitiesResponse>"
        )
    if "GetEventProperties" in xml:
        return soap(TOPICS)
    if "CreatePullPointSubscription" in xml:
        sub_id = str(len(queues) + 1)
        t = time.time()
        queues[sub_id] = [
            (t, "Initialized", "motion", False),
            (t, "Initialized", "person", False),
            *((t + 3 + i * 0.2, "Initialized", "motion", True) for i in range(10)),
            (t + 4, "Initialized", "person", True),
            (t + 7, "Initialized", "person", False),
            (t + 8, "Initialized", "motion", False),
        ]
        return soap(
            "<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
            f"<wsa:Address>{BASE}/onvif/subscription/{sub_id}</wsa:Address>"
            "</tev:SubscriptionReference></tev:CreatePullPointSubscriptionResponse>"
        )
    if "PullMessages" in xml:
        queue = queues.get(path.rsplit("/", 1)[-1])
        if queue is None:
            return fault("Unknown subscription")
        deadline = time.time() + int(re.search(r"PT(\d+)S", xml).group(1))
        while time.time() < deadline and not any(due <= time.time() for due, *_ in queue):
            await asyncio.sleep(0.2)
        ready = [e for e in queue if e[0] <= time.time()]
        for e in ready:
            queue.remove(e)
        return soap("<tev:PullMessagesResponse>" + "".join(notification(*e[1:]) for e in ready) + "</tev:PullMessagesResponse>")
    if "Renew" in xml:
        return soap("<wsnt:RenewResponse/>")
    if "Unsubscribe" in xml:
        return soap("<wsnt:UnsubscribeResponse/>")
    return fault("Not supported by the mock")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")
