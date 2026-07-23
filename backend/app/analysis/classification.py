"""Pluggable transcript analysis (ARCHITECTURE.md #5): spam + category + tags + summary.

Spam here means "the caller is selling/soliciting" — judged from transcript *content*, not
a phone-reputation API. `dummy` is a keyword heuristic (offline/testable); `claude` uses
Claude Haiku with tool-use for guaranteed-structured output.

"Job" detection: a caller who *gives their address* AND *asks about a service* (roofing,
garage door, etc.) is a real lead, not a random call. Each engine reports two booleans plus
an optional service type; `job_tags()` turns them into the free-form tags `job` and
`service:<type>`, which render as badges on the call and are queryable in the JSONB column.
No new schema — the signal lives in the existing free-form `tags` (ARCHITECTURE.md #5).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("analysis.classification")

# Controlled category vocabulary (chartable). Free-form `tags` carry the nuance.
CATEGORIES = [
    "sales-spam", "pricing", "booking", "support", "complaint", "wrong-number", "other",
]
_SPAM_KEYWORDS = ["sell", "selling", "offer", "warranty", "promotion", "discount",
                  "insurance", "special deal", "limited time", "extend"]

# Free-form tag emitted when a call qualifies as a job/lead (address + service request).
JOB_TAG = "job"

# Heuristic vocab for the offline `dummy` engine only. The real engines let the LLM judge.
_SERVICE_KEYWORDS = {
    "roofing": ["roof", "roofing", "shingle", "gutter"],
    "garage-door": ["garage door", "garage-door", "overhead door"],
    "hvac": ["hvac", "air conditioning", "furnace", "heating", "ac unit", "a/c"],
    "plumbing": ["plumb", "leak", "water heater", "drain", "faucet"],
    "electrical": ["electric", "wiring", "outlet", "breaker"],
    "landscaping": ["landscap", "lawn", "yard work", "tree removal"],
}
# Address cues: a street-number+name ("123 Main St") or a 5-digit ZIP.
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z][A-Za-z0-9.\s]{1,30}?"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|way|place|pl)\b"
    r"|\b\d{5}(?:-\d{4})?\b",
    re.IGNORECASE,
)


def normalize_service_type(service_type: str | None) -> str | None:
    """Lower-case, dash-separate a free-text service label so tags stay consistent
    (`Garage Door` -> `garage-door`). Returns None for empty/whitespace input."""
    slug = re.sub(r"[^a-z0-9]+", "-", (service_type or "").strip().lower()).strip("-")
    return slug or None


def job_tags(gave_address: bool, requested_service: bool,
             service_type: str | None = None) -> list[str]:
    """Derive the job/lead tags from the three signals. A call is a job only when the
    caller both gave an address and asked about a service — either alone is not a lead.
    Returns `["job"]` (plus `service:<type>` when a type is known), else `[]`."""
    if not (gave_address and requested_service):
        return []
    tags = [JOB_TAG]
    slug = normalize_service_type(service_type)
    if slug:
        tags.append(f"service:{slug}")
    return tags


def _merge_tags(base: list[str], extra: list[str]) -> list[str]:
    """Append `extra` tags not already present, preserving order (no duplicate badges)."""
    out = list(base)
    for t in extra:
        if t not in out:
            out.append(t)
    return out


@dataclass
class AnalysisResult:
    is_spam: bool
    spam_confidence: float
    category: str
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    model: str = ""


class AnalysisEngine(Protocol):
    name: str

    async def analyze(self, transcript: str) -> AnalysisResult: ...


class DummyAnalysisEngine:
    name = "dummy"

    async def analyze(self, transcript: str) -> AnalysisResult:
        low = (transcript or "").lower()
        hits = [k for k in _SPAM_KEYWORDS if k in low]
        is_spam = bool(hits)

        # Offline job heuristic: an address cue + a recognized service request.
        gave_address = bool(_ADDRESS_RE.search(transcript or ""))
        service_type = next(
            (svc for svc, kws in _SERVICE_KEYWORDS.items() if any(k in low for k in kws)),
            None,
        )
        job = job_tags(gave_address, service_type is not None, service_type)

        tags = _merge_tags(hits or ["uncategorized"], job)
        return AnalysisResult(
            is_spam=is_spam,
            spam_confidence=0.9 if is_spam else 0.1,
            # A genuine job is a booking-type lead, not spam/other.
            category="sales-spam" if is_spam else ("booking" if job else "other"),
            tags=tags,
            summary=(transcript or "")[:160],
            model="dummy",
        )


_TOOL = {
    "name": "record_analysis",
    "description": "Return the structured analysis of a phone call transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_spam": {"type": "boolean",
                        "description": "True if the caller is selling/soliciting (unwanted sales)."},
            "spam_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "category": {"type": "string", "enum": CATEGORIES},
            "tags": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string", "description": "One or two sentences."},
            "gave_address": {"type": "boolean",
                             "description": "True if the CALLER stated a service/property "
                             "address (street address or ZIP), not just a city."},
            "requested_service": {"type": "boolean",
                                  "description": "True if the caller asked about or requested a "
                                  "home service (e.g. roofing, garage door, HVAC, plumbing)."},
            "service_type": {"type": "string",
                             "description": "Short label of the service requested, e.g. "
                             "'roofing', 'garage door'. Empty string if none."},
        },
        "required": ["is_spam", "spam_confidence", "category", "tags", "summary",
                     "gave_address", "requested_service", "service_type"],
    },
}

_JOB_INSTRUCTIONS = (
    "Also determine whether this is a JOB / service lead: set gave_address=true only if the "
    "caller gives a specific service or property address (street address or ZIP), and "
    "requested_service=true only if they ask about or request a home service. When they ask "
    "about a service, put a short service_type label (e.g. 'roofing', 'garage door'); "
    "otherwise leave service_type empty."
)


def _parse_tool_json(raw: str) -> dict:
    """Parse a function-call JSON payload, tolerating the truncated/malformed output some
    OpenAI-compatible providers (MiniMax) occasionally emit: try a straight parse, then
    attempt to repair a truncated tail (unterminated string / unbalanced brackets) before
    giving up. Raises json.JSONDecodeError if the payload still can't be salvaged."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    repaired = raw
    if repaired.count('"') % 2 == 1:
        repaired += '"'
    repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
    repaired += "]" * max(0, repaired.count("[") - repaired.count("]"))
    return json.loads(repaired)


def _result_from_tool_input(data: dict, model: str) -> AnalysisResult:
    """Build an AnalysisResult from a validated tool-call payload, folding the job/lead
    signal into the free-form tags. Shared by the Claude and MiniMax engines."""
    tags = _merge_tags(
        list(data.get("tags", [])),
        job_tags(bool(data.get("gave_address")), bool(data.get("requested_service")),
                 data.get("service_type")),
    )
    return AnalysisResult(
        is_spam=bool(data["is_spam"]),
        spam_confidence=float(data["spam_confidence"]),
        category=data["category"] if data["category"] in CATEGORIES else "other",
        tags=tags,
        summary=data.get("summary", ""),
        model=model,
    )


class ClaudeAnalysisEngine:
    name = "claude"

    async def analyze(self, transcript: str) -> AnalysisResult:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        prompt = (
            "Analyze this phone call transcript. Decide if it is unwanted sales/solicitation "
            "(is_spam), assign one category, add a few descriptive tags, and summarize.\n"
            f"{_JOB_INSTRUCTIONS}\n\n"
            f"Transcript:\n{transcript}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.ANALYSIS_MODEL,
                    "max_tokens": 512,
                    "tools": [_TOOL],
                    "tool_choice": {"type": "tool", "name": "record_analysis"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        tool_use = next((b for b in blocks if b.get("type") == "tool_use"), None)
        data = tool_use["input"] if tool_use else json.loads(blocks[0]["text"])
        return _result_from_tool_input(data, settings.ANALYSIS_MODEL)


class MinimaxAnalysisEngine:
    """MiniMax chat completions (OpenAI-compatible function-calling)."""

    name = "minimax"

    async def analyze(self, transcript: str) -> AnalysisResult:
        if not settings.MINIMAX_API_KEY:
            raise RuntimeError("MINIMAX_API_KEY not set")
        prompt = (
            "Analyze this phone call transcript. Decide if it is unwanted sales/solicitation "
            "(is_spam), assign one category, add a few descriptive tags, and summarize.\n"
            f"{_JOB_INSTRUCTIONS}\n\n"
            f"Transcript:\n{transcript}"
        )
        tool = {"type": "function", "function": {
            "name": _TOOL["name"],
            "description": _TOOL["description"],
            "parameters": _TOOL["input_schema"],
        }}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.MINIMAX_BASE_URL}/text/chatcompletion_v2",
                headers={
                    "Authorization": f"Bearer {settings.MINIMAX_API_KEY}",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.ANALYSIS_MODEL,
                    "max_tokens": 512,
                    "tools": [tool],
                    "tool_choice": {"type": "function", "function": {"name": _TOOL["name"]}},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        raw = tool_calls[0]["function"]["arguments"] if tool_calls else message.get("content", "")
        try:
            data = _parse_tool_json(raw)
        except json.JSONDecodeError:
            # MiniMax occasionally emits malformed/truncated function-call JSON. Falling
            # back to the offline heuristic keeps the job (and the call's analysis row)
            # alive instead of exhausting retries and dead-lettering permanently — logged
            # loudly so it stays visible rather than silently degrading quality.
            logger.warning(
                "minimax analyze: malformed tool-call JSON, falling back to heuristic engine: %r",
                raw[:300],
            )
            result = await DummyAnalysisEngine().analyze(transcript)
            result.model = "minimax-fallback"
            return result
        return _result_from_tool_input(data, settings.ANALYSIS_MODEL)


_ENGINES = {"dummy": DummyAnalysisEngine, "claude": ClaudeAnalysisEngine, "minimax": MinimaxAnalysisEngine}


def get_analysis_engine() -> AnalysisEngine:
    return _ENGINES.get(settings.ANALYSIS_ENGINE, DummyAnalysisEngine)()
