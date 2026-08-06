"""`GET /api/ai` and `GET /api/ai/docs` — how a caller that knows only the URL bootstraps itself.

An external integration has no access to this repository, so the documentation has to be part
of the API. `/api/ai` returns a compact machine-readable index (endpoints, scopes, periods,
the caveats that most often produce wrong answers); `/api/ai/docs` returns the full manual.

The manual is `AI_API.md`, sitting next to this file — one file, read from disk at request
time. Serving the same file the repository contains is what stops the docs and the code from
drifting: there is no second copy to forget to update.

Both are readable with ANY valid key regardless of scope. A caller must be able to discover
that it lacks a scope, and to look up how to use what it does have.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Response

from app.api.ai.deps import AuthedKey, authenticate
from app.api.ai.envelope import NOTE_JUNK, NOTE_PHANTOM, NOTE_SPAM_DEAD
from app.api.ai.periods import DEFAULT_PERIOD, PERIODS
from app.core.apikeys import SCOPES
from app.core.config import settings

router = APIRouter(prefix="/api/ai", tags=["ai"])

DOCS_PATH = Path(__file__).with_name("AI_API.md")

ENDPOINTS: list[dict] = [
    {"method": "GET", "path": "/api/ai", "scope": "any",
     "purpose": "This index."},
    {"method": "GET", "path": "/api/ai/docs", "scope": "any",
     "purpose": "Full manual in Markdown, with worked examples. Read this first."},
    {"method": "GET", "path": "/api/ai/calls/stats", "scope": "read",
     "purpose": "Call volume and duration for any window and filter combination.",
     "key_params": ["period", "date_from", "date_to", "min_duration", "max_duration",
                    "campaign", "number", "answered", "new_callers", "include_junk", "group_by"]},
    {"method": "GET", "path": "/api/ai/calls/top-callers", "scope": "read",
     "purpose": "Who called most in the window."},
    {"method": "GET", "path": "/api/ai/calls/categories", "scope": "read",
     "purpose": "AI category mix for analyzed calls."},
    {"method": "GET", "path": "/api/ai/leads/stats", "scope": "read",
     "purpose": "New leads from Dispatch/American Home Shield job emails, per day or week.",
     "key_params": ["period", "source", "group_by"]},
    {"method": "GET", "path": "/api/ai/messages/stats", "scope": "read",
     "purpose": "SMS/MMS volume."},
    {"method": "GET", "path": "/api/ai/billing/summary", "scope": "read",
     "purpose": "Telephony spend from BulkVS rated records."},
    {"method": "GET", "path": "/api/ai/health/pipeline", "scope": "read",
     "purpose": "Is anything broken right now: ingestion freshness, queue depth, dead jobs, relays."},
    {"method": "GET", "path": "/api/ai/flows/outcomes", "scope": "read",
     "purpose": "What the IVR did with callers: how calls ended, which menu ports were taken "
                "(timeout vs invalid digit), dial results, and how many callers were DROPPED "
                "by an unwired port. A dropped caller looks like a normal completed call "
                "everywhere else, so this is the only place the loss is visible.",
     "key_params": ["period", "date_from", "date_to", "flow"]},
    {"method": "GET", "path": "/api/ai/flows/calls", "scope": "read",
     "purpose": "Drill-down: individual calls with their node path and end reason. Use "
                "ended=unrouted_hangup to list the specific callers who were dropped.",
     "key_params": ["period", "ended", "limit"]},
    {"method": "GET", "path": "/api/ai/errors", "scope": "logs",
     "purpose": "Captured warnings/errors, failed jobs and failed relays in one list.",
     "key_params": ["since", "source", "level", "service", "linkedid", "limit"]},
    {"method": "GET", "path": "/api/ai/calls/recent", "scope": "content",
     "purpose": "Individual calls with their AI summary and category."},
    {"method": "GET", "path": "/api/ai/calls/{call_id}/transcript", "scope": "content",
     "purpose": "Full transcript and analysis for one call."},
    {"method": "GET", "path": "/api/ai/leads/recent", "scope": "content",
     "purpose": "Individual leads with extracted customer details."},
    {"method": "GET", "path": "/api/ai/schema", "scope": "read",
     "purpose": "Live database schema plus the caveats needed to write a correct query."},
    {"method": "POST", "path": "/api/ai/query", "scope": "sql + content",
     "purpose": "Run one read-only SQL statement. Body: {\"sql\": \"...\", \"limit\": 200}."},
]


@router.get("")
async def index(key: AuthedKey = Depends(authenticate)) -> dict:
    """Machine-readable index. Start here, then GET /api/ai/docs."""
    return {
        "service": "OWEN AI API",
        "description": (
            "Read-only access to OWEN, an ad/campaign call-attribution platform: inbound calls "
            "and their recordings/transcripts, leads from job-notification emails, SMS, "
            "telephony spend, and operational errors."
        ),
        "authentication": {
            "header": "X-OWEN-Key: owen_sk_...",
            "alternative": "Authorization: Bearer owen_sk_...",
            "note": "Keys are issued in the OWEN UI under API Keys and are read-only.",
        },
        "your_key": {"name": key.name, "scopes": key.scopes},
        "scopes": SCOPES,
        "endpoints": ENDPOINTS,
        "time": {
            "business_timezone": settings.BUSINESS_TZ,
            "default_period": DEFAULT_PERIOD,
            "named_periods": PERIODS,
            "note": "Every endpoint accepts `period=` or explicit `date_from`/`date_to`. "
                    "Named periods resolve in the business timezone; the resolved UTC bounds "
                    "come back in `applied_filters`.",
        },
        "response_shape": {
            "summary": "A plain-English sentence you can quote verbatim and still be correct.",
            "data": "The numbers.",
            "applied_filters": "Exactly what was counted, including resolved UTC bounds.",
            "notes": "Caveats that apply to this answer. Do not drop these when reporting.",
        },
        "read_this_first": [NOTE_PHANTOM, NOTE_JUNK, NOTE_SPAM_DEAD],
        "docs": "/api/ai/docs",
    }


@router.get("/docs")
async def docs(_: AuthedKey = Depends(authenticate)) -> Response:
    """The full manual, as Markdown."""
    try:
        body = DOCS_PATH.read_text(encoding="utf-8")
    except OSError:
        # The index is a genuine fallback, not a stub: it carries the endpoint list, the
        # scopes and the caveats, which is enough to use the API correctly.
        body = ("# OWEN AI API\n\nThe manual file is missing from this deployment. "
                "GET /api/ai returns the machine-readable index, which covers every endpoint.\n")
    return Response(content=body, media_type="text/markdown; charset=utf-8")
