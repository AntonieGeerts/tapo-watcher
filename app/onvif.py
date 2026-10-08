"""Minimal ONVIF client: just enough SOAP to follow a camera's PullPoint event feed.

Tapo cameras serve ONVIF on port 2020 and authenticate with the "Camera Account" set in
the Tapo app (camera > Settings > Advanced Settings > Camera Account).
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "a": "http://www.w3.org/2005/08/addressing",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
}
DEVICE_WSDL = NS["tds"]
EVENTS_WSDL = NS["tev"]
SUBSCRIPTION_MANAGER = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_BINARY = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class OnvifError(Exception):
    """A SOAP fault, HTTP error or unexpected response from the camera."""


@dataclass
class Notification:
    topic: str  # e.g. "tns1:RuleEngine/PeopleDetector/People"
    operation: str  # Initialized | Changed | Deleted
    data: dict[str, str]  # e.g. {"IsPeople": "true"}
    source: dict[str, str]
    utc_time: str | None


@dataclass
class Subscription:
    address: str
    reference_headers: str  # ReferenceParameters, echoed back as SOAP headers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_notification(el: ET.Element) -> Notification:
    topic = (el.findtext("wsnt:Topic", default="", namespaces=NS) or "").strip()
    msg = el.find(".//tt:Message", NS)
    if msg is None:
        return Notification(topic, "", {}, {}, None)

    def items(path: str) -> dict[str, str]:
        return {i.get("Name", ""): i.get("Value", "") for i in msg.iterfind(path, NS)}

    return Notification(
        topic=topic,
        operation=msg.get("PropertyOperation", ""),
        data=items("tt:Data/tt:SimpleItem"),
        source=items("tt:Source/tt:SimpleItem"),
        utc_time=msg.get("UtcTime"),
    )


class OnvifClient:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.user = user
        self.password = password
        self.clock_offset = 0.0  # camera clock minus ours, in seconds
        self.http = httpx.AsyncClient(timeout=15)

    async def aclose(self) -> None:
        await self.http.aclose()

    def _security_header(self) -> str:
        nonce = os.urandom(16)
        created = datetime.fromtimestamp(time.time() + self.clock_offset, timezone.utc)
        created_text = created.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(nonce + created_text.encode() + self.password.encode()).digest()
        return (
            f'<Security s:mustUnderstand="1" xmlns="{WSSE}"><UsernameToken>'
            f"<Username>{escape(self.user)}</Username>"
            f'<Password Type="{PASSWORD_DIGEST}">{base64.b64encode(digest).decode()}</Password>'
            f'<Nonce EncodingType="{BASE64_BINARY}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{WSU}">{created_text}</Created>'
            "</UsernameToken></Security>"
        )

    async def _call(
        self,
        url: str,
        action: str,
        body: str,
        *,
        auth: bool = True,
        addressing: bool = False,
        extra_headers: str = "",
        timeout: float = 15,
    ) -> ET.Element:
        headers = self._security_header() if auth else ""
        if addressing:
            headers += (
                f"<a:Action>{escape(action)}</a:Action>"
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
                f"<a:To>{escape(url)}</a:To>{extra_headers}"
            )
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:a="{NS["a"]}">'
            f"<s:Header>{headers}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        try:
            resp = await self.http.post(
                url,
                content=envelope.encode(),
                headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise OnvifError(f"cannot reach {url}: {e.__class__.__name__}") from e
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise OnvifError(f"HTTP {resp.status_code} with a non-XML body from {url}") from e
        fault = root.find(".//s:Fault", NS)
        if fault is not None or resp.status_code >= 400:
            detail = " ".join(t.strip() for t in fault.itertext() if t.strip()) if fault is not None else ""
            raise OnvifError(f"HTTP {resp.status_code} from camera: {detail or 'no details'}")
        body_el = root.find("s:Body", NS)
        if body_el is None:
            raise OnvifError("SOAP response without a Body")
        return body_el

    async def sync_clock(self) -> None:
        """Match the camera's clock, otherwise it rejects our WS-Security timestamps."""
        body = await self._call(
            self.device_url,
            f"{DEVICE_WSDL}/GetSystemDateAndTime",
            f'<GetSystemDateAndTime xmlns="{DEVICE_WSDL}"/>',
            auth=False,
        )
        utc = body.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return

        def part(path: str) -> int:
            return int(utc.findtext(path, default="0", namespaces=NS))

        try:
            camera_now = datetime(
                part("tt:Date/tt:Year"), part("tt:Date/tt:Month"), part("tt:Date/tt:Day"),
                part("tt:Time/tt:Hour"), part("tt:Time/tt:Minute"), part("tt:Time/tt:Second"),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return
        self.clock_offset = camera_now.timestamp() - time.time()

    async def events_url(self) -> str:
        body = await self._call(
            self.device_url,
            f"{DEVICE_WSDL}/GetCapabilities",
            f'<GetCapabilities xmlns="{DEVICE_WSDL}"><Category>Events</Category></GetCapabilities>',
        )
        xaddr = body.findtext(".//tt:Events/tt:XAddr", namespaces=NS)
        if not xaddr:
            raise OnvifError("camera does not advertise an ONVIF events service")
        return xaddr.strip()

    async def event_topics(self, events_url: str) -> list[str]:
        """Topics the camera says it can send, e.g. "RuleEngine/PeopleDetector/People [IsPeople]"."""
        body = await self._call(
            events_url,
            f"{EVENTS_WSDL}/EventPortType/GetEventPropertiesRequest",
            f'<GetEventProperties xmlns="{EVENTS_WSDL}"/>',
            addressing=True,
        )
        topics: list[str] = []

        def walk(el: ET.Element, path: list[str]) -> None:
            for child in el:
                name = _local(child.tag)
                if name == "MessageDescription":
                    continue
                child_path = [*path, name]
                if child.get(f"{{{NS['wstop']}}}topic") == "true":
                    items = [i.get("Name", "") for i in child.iterfind(".//tt:Data/tt:SimpleItemDescription", NS)]
                    topics.append("/".join(child_path) + (f" [{', '.join(items)}]" if items else ""))
                walk(child, child_path)

        topic_set = body.find(".//wstop:TopicSet", NS)
        if topic_set is not None:
            walk(topic_set, [])
        return topics

    async def subscribe(self, events_url: str, lifetime: str = "PT600S") -> Subscription:
        body = await self._call(
            events_url,
            f"{EVENTS_WSDL}/EventPortType/CreatePullPointSubscriptionRequest",
            f'<CreatePullPointSubscription xmlns="{EVENTS_WSDL}">'
            f"<InitialTerminationTime>{lifetime}</InitialTerminationTime></CreatePullPointSubscription>",
            addressing=True,
        )
        ref = body.find(".//tev:SubscriptionReference", NS)
        if ref is None:
            raise OnvifError("CreatePullPointSubscription returned no subscription")
        address = next((el.text.strip() for el in ref if _local(el.tag) == "Address" and el.text), None)
        if not address:
            raise OnvifError("CreatePullPointSubscription returned no address")
        params = next((el for el in ref if _local(el.tag) == "ReferenceParameters"), None)
        headers = "".join(ET.tostring(c, encoding="unicode") for c in params) if params is not None else ""
        return Subscription(address, headers)

    async def pull(self, sub: Subscription, wait: int = 5, limit: int = 50) -> list[Notification]:
        """Wait up to `wait` seconds for events. Keep it short: Tapo cameras drop a
        PullMessages connection after about 10 seconds instead of answering it."""
        body = await self._call(
            sub.address,
            f"{EVENTS_WSDL}/PullPointSubscription/PullMessagesRequest",
            f'<PullMessages xmlns="{EVENTS_WSDL}">'
            f"<Timeout>PT{wait}S</Timeout><MessageLimit>{limit}</MessageLimit></PullMessages>",
            addressing=True,
            extra_headers=sub.reference_headers,
            timeout=wait + 15,
        )
        return [_parse_notification(n) for n in body.iterfind(".//wsnt:NotificationMessage", NS)]

    async def renew(self, sub: Subscription, lifetime: str = "PT600S") -> None:
        await self._call(
            sub.address,
            f"{SUBSCRIPTION_MANAGER}/RenewRequest",
            f'<Renew xmlns="{NS["wsnt"]}"><TerminationTime>{lifetime}</TerminationTime></Renew>',
            addressing=True,
            extra_headers=sub.reference_headers,
        )

    async def unsubscribe(self, sub: Subscription) -> None:
        await self._call(
            sub.address,
            f"{SUBSCRIPTION_MANAGER}/UnsubscribeRequest",
            f'<Unsubscribe xmlns="{NS["wsnt"]}"/>',
            addressing=True,
            extra_headers=sub.reference_headers,
            timeout=5,
        )
