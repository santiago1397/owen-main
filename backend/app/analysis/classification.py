"""Pluggable transcript analysis (ARCHITECTURE.md #5): spam + category + tags + summary.

Spam here means "the caller is selling/soliciting" — judged from transcript *content*, not
a phone-reputation API. `dummy` is a keyword heuristic (offline/testable); `claude` uses
Claude Haiku with tool-use for guaranteed-structured output.
"""

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import settings

# Controlled category vocabulary (chartable). Free-form `tags` carry the nuance.
CATEGORIES = [
    "sales-spam", "pricing", "booking", "support", "complaint", "wrong-number", "other",
]
_SPAM_KEYWORDS = ["sell", "selling", "offer", "warranty", "promotion", "discount",
                  "insurance", "special deal", "limited time", "extend"]


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
        return AnalysisResult(
            is_spam=is_spam,
            spam_confidence=0.9 if is_spam else 0.1,
            category="sales-spam" if is_spam else "other",
            tags=hits or ["uncategorized"],
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
        },
        "required": ["is_spam", "spam_confidence", "category", "tags", "summary"],
    },
}


class ClaudeAnalysisEngine:
    name = "claude"

    async def analyze(self, transcript: str) -> AnalysisResult:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        prompt = (
            "Analyze this phone call transcript. Decide if it is unwanted sales/solicitation "
            "(is_spam), assign one category, add a few descriptive tags, and summarize.\n\n"
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
        return AnalysisResult(
            is_spam=bool(data["is_spam"]),
            spam_confidence=float(data["spam_confidence"]),
            category=data["category"] if data["category"] in CATEGORIES else "other",
            tags=list(data.get("tags", [])),
            summary=data.get("summary", ""),
            model=settings.ANALYSIS_MODEL,
        )


class MinimaxAnalysisEngine:
    """MiniMax chat completions (OpenAI-compatible function-calling)."""

    name = "minimax"

    async def analyze(self, transcript: str) -> AnalysisResult:
        if not settings.MINIMAX_API_KEY:
            raise RuntimeError("MINIMAX_API_KEY not set")
        prompt = (
            "Analyze this phone call transcript. Decide if it is unwanted sales/solicitation "
            "(is_spam), assign one category, add a few descriptive tags, and summarize.\n\n"
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
        if tool_calls:
            data = json.loads(tool_calls[0]["function"]["arguments"])
        else:
            data = json.loads(message["content"])
        return AnalysisResult(
            is_spam=bool(data["is_spam"]),
            spam_confidence=float(data["spam_confidence"]),
            category=data["category"] if data["category"] in CATEGORIES else "other",
            tags=list(data.get("tags", [])),
            summary=data.get("summary", ""),
            model=settings.ANALYSIS_MODEL,
        )


_ENGINES = {"dummy": DummyAnalysisEngine, "claude": ClaudeAnalysisEngine, "minimax": MinimaxAnalysisEngine}


def get_analysis_engine() -> AnalysisEngine:
    return _ENGINES.get(settings.ANALYSIS_ENGINE, DummyAnalysisEngine)()
