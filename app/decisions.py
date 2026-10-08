"""Asks the OpenAI Decisions API whether a frame shows a person, and whether anyone in it is
using a mobile phone. https://developers.openai.com/api/docs/guides/decisions
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import httpx

from .config import unset_placeholder

API_URL = "https://api.openai.com/v1/decisions"
OUTCOMES = ("phone", "no_phone", "unclear")
QUESTIONS = [
    {
        "type": "predicate",
        "name": "person_visible",
        "instructions": "Is at least one person visible in this home security camera frame?",
    },
    {
        "type": "choice",
        "name": "phone_use",
        "instructions": (
            "Is any person in this home security camera frame using a mobile phone, for example "
            "looking at it, typing on it, or holding it to their ear?"
        ),
        "choices": [
            {"value": "phone", "description": "A person is holding or using a mobile phone."},
            {
                "value": "no_phone",
                "description": "People are visible and none of them is holding or using a mobile phone.",
            },
            {
                "value": "unclear",
                "description": "No person is visible, or their hands and head are too small, dark, "
                "blurred or hidden to tell.",
            },
        ],
    },
]


class DecisionsError(Exception):
    pass


def read_api_key(path: Path) -> str | None:
    """OPENAI_API_KEY wins; otherwise the first line of the key file (bare key or NAME=key)."""
    if key := unset_placeholder(os.environ.get("OPENAI_API_KEY")):
        return key
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        value = line.split("=", 1)[-1].strip().strip("'\"")
        if value:
            return value
    return None


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:300]


class DecisionsClient:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.http = httpx.AsyncClient(timeout=45, headers={"Authorization": f"Bearer {api_key}"})

    async def ask(self, jpeg: bytes) -> dict[str, dict]:
        """Answers for one frame, keyed by question name."""
        payload = {
            "model": self.model,
            "input": [{
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "A frame from a home security camera, captured when it detected movement.",
                    },
                    {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
                ],
            }],
            "questions": QUESTIONS,
        }
        for attempt in range(3):
            try:
                resp = await self.http.post(API_URL, json=payload)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise DecisionsError(f"request failed: {e.__class__.__name__}") from e
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise DecisionsError(f"HTTP {resp.status_code}: {_error_message(resp)}")
            return {a.get("name"): a for a in resp.json().get("answers", [])}
        raise DecisionsError("no response")

    async def ask_frame(self, frame: Path) -> dict:
        """{"frame": name, "answers": ...} or {"frame": name, "error": ...}; never raises."""
        try:
            return {"frame": frame.name, "answers": await self.ask(frame.read_bytes())}
        except Exception as e:
            return {"frame": frame.name, "error": str(e) or e.__class__.__name__}


def _frame_summary(result: dict) -> dict:
    if "error" in result:
        return {"frame": result["frame"], "error": result["error"]}
    answers = result["answers"]
    summary: dict = {"frame": result["frame"], "person": None}
    person = answers.get("person_visible", {})
    if person.get("type") == "predicate":
        summary["person"] = round(float(person["probability"]), 3)
    phone = answers.get("phone_use", {})
    if phone.get("type") == "choice":
        probs = {p["value"]: round(float(p["probability"]), 3) for p in phone.get("probabilities") or []}
        summary["probs"] = probs or {phone.get("choice"): round(float(phone.get("confidence", 1.0)), 3)}
    if "refusal" in (person.get("type"), phone.get("type")):
        summary["refused"] = True
    return summary


def summarize(results: list[dict], person_min: float, verdict_min: float) -> dict:
    """Combine per-frame answers into one verdict for the event.

    Frames that probably show a person are averaged, weighted by how sure the model is that
    a person is there. If neither "phone" nor "no_phone" reaches verdict_min the verdict is
    "unclear".
    """
    frames = [_frame_summary(r) for r in results]
    answered = [f for f in frames if "error" not in f]
    if not answered:
        return {"label": "error", "error": frames[0]["error"] if frames else "no frames analysed", "frames": frames}

    best = max(answered, key=lambda f: f["person"] or 0)
    verdict: dict = {"label": "no_person", "person_probability": best["person"], "best_frame": best["frame"], "frames": frames}
    with_person = [f for f in answered if (f["person"] or 0) >= person_min]
    if not with_person:
        if any(f.get("refused") for f in answered):
            verdict["label"] = "refused"
        return verdict

    judged = [f for f in with_person if f.get("probs")]
    if not judged:
        verdict["label"] = "refused" if any(f.get("refused") for f in with_person) else "unclear"
        return verdict
    total = sum(f["person"] for f in judged)
    probs = {o: round(sum(f["person"] * f["probs"].get(o, 0.0) for f in judged) / total, 3) for o in OUTCOMES}
    label = max(("phone", "no_phone"), key=probs.__getitem__)
    verdict["probabilities"] = probs
    if probs[label] >= verdict_min:
        # Show the picture that makes the verdict clearest.
        best = max(judged, key=lambda f: f["probs"].get(label, 0.0))
        verdict.update(label=label, probability=probs[label], best_frame=best["frame"])
    else:
        verdict["label"] = "unclear"
    return verdict
