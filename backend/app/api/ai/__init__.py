"""The AI API: read-only, API-key-authed access to OWEN for machine callers.

Split by concern rather than by resource, because the concerns have different security
properties:

- `deps`     — key auth, scope gating, rate limiting, usage audit (the way in)
- `envelope` — the response shape, and the caveats attached to every answer
- `periods`  — named time windows resolved in the business timezone
- `filters`  — what counts as a call (the phantom-row and junk predicates)
- `metrics`  — curated aggregate endpoints                      [scope: read]
- `health`   — pipeline health                                  [scope: read]
- `flows`    — IVR outcomes: where callers went, and who got dropped   [scope: read]
- `content`  — transcripts, summaries, customer details         [scope: content]
- `errors`   — captured logs, dead jobs, failed relays          [scope: logs]
- `schema`   — live DB reference for writing SQL                [scope: read]
- `query`    — guarded read-only SQL                            [scope: sql + content]
- `root`     — self-describing index and the manual             [scope: any]
"""

from fastapi import APIRouter

from app.api.ai import content, errors, flows, health, metrics, query, root, schema

router = APIRouter()
# `root` is included first so `/api/ai` and `/api/ai/docs` resolve before any
# parameterized path in the other modules could shadow them.
router.include_router(root.router)
router.include_router(metrics.router)
router.include_router(health.router)
router.include_router(flows.router)
router.include_router(content.router)
router.include_router(errors.router)
router.include_router(schema.router)
router.include_router(query.router)

__all__ = ["router"]
