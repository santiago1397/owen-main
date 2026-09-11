"""OpenPhone mirror — activity IN, nothing OUT. Ever.

A self-contained, opt-in, READ-ONLY mirror of the company's OpenPhone line into the
`ghl-clone` CRM, so a customer who called OpenPhone last month and the BulkVS number this
morning has ONE timeline instead of two. OpenPhone is the system the business is migrating
AWAY from; this makes its history and its new activity visible without keeping anyone in
two apps.

Everything it does is off unless:

    OPENPHONE_MIRROR_ENABLED=true                (the kill switch, default false)
    OPENPHONE_API_KEY is set                     (no key, no reads — and no 401 storm)

With either missing, nothing is scheduled, no request is made, and every route on the
`/api/openphone-mirror` router answers 503. That is not an aspiration — it is what
`tests/test_openphone_mirror.py` asserts by counting outbound requests.

## THE RULE THIS MODULE IS BUILT AROUND

**OpenPhone is READ-ONLY. This mirror can never send a text, place a call, or answer one.**

It is the rule `providers/openphone_client.py` already states in its own header and
`docs/GHL_SYNC_SPEC.md` D16 records as a decision, and the reason is that in OpenPhone a
stray `POST /messages` does not fail a test, it texts a real customer and bills for it.

The guarantee is structural, not careful:

  * `openphone_client` exposes `_get` and nothing else. There is no `_post`/`_put`/`_delete`
    helper in the file to misuse, and `_get` refuses action-shaped paths before the request
    leaves the process.
  * Every function this module added to that client is a GET.
  * `test_openphone_mirror.py` monkeypatches `httpx.AsyncClient` and asserts that a full
    backfill — enumeration, calls, messages, transcripts, summaries, recordings — issues
    **zero non-GET requests to api.openphone.com**. That is the most important test here.
  * The composer in the CRM is unchanged and still sends over BulkVS. There is no OpenPhone
    send path anywhere, not even a disabled one, because a disabled send path is a switch
    somebody eventually flips.

## What is in here

| module      | what it is                                                              |
|-------------|--------------------------------------------------------------------------|
| `config.py` | PURE kernel: the kill switch, which lines are mirrored, the dedupe key   |
| `models.py` | `openphone_mirror_rows` — what has already been handed to the CRM       |
| `events.py` | PURE: an OpenPhone call/text -> the CRM's `POST /api/events` body        |
| `sync.py`   | the poll: who to ask about, what is new, and what to do when it fails   |
| `push.py`   | queues onto the EXISTING `crm_report` job (retry/backoff for free)      |
| `api.py`    | `/api/openphone-mirror/*`: the delivery hop and the recording stream    |
| `manage.py` | `python -m app.integrations.openphone.manage` — status/preview/backfill |

## Polling, not webhooks

Registering an OpenPhone webhook requires `POST /v1/webhooks`, and the signing secret is
returned only in that call's response. Both halves disqualify it here: the POST is the exact
write this module may not make, and without the secret a webhook's signature could not be
verified — so the "webhooks if you can verify signatures properly" precondition fails on its
own terms. `sync.py`'s docstring carries the full argument and the seam left for a future
receiver.

## Files OUTSIDE this package that were touched, and why

  * `providers/openphone_client.py` — three GET readers (`list_messages`,
    `list_conversations`, `fetch_recording_bytes`). No new transport helper; `_get` is
    still the only way out. This is the one existing file with real risk attached, which is
    why the no-write test targets it directly.
  * `core/config.py` — the new `OPENPHONE_MIRROR_*` settings, all defaulting to off/empty.
  * `models/__init__.py` — imports `OpenPhoneMirrorRow` so `alembic/env.py` sees the table.
    Without it the next `--autogenerate` would emit `DROP TABLE openphone_mirror_rows`.
  * `main.py` — one `include_router`. Every route 503s while disabled.
  * `worker.py` — one `if enabled():` scheduler block, matching `bulkvs_sync`. Nothing is
    scheduled while the switch is off, so the worker does not even wake up for it.
  * `api/ai/deps.py` — `/api/openphone-mirror` added to the audited path prefixes.
  * `scripts/probe_openphone.py` — two new read-only probes, for the two endpoints the
    original probe did not cover.
  * `alembic/versions/` — one CREATE TABLE. No ALTER, no DROP.

**No existing call path changed.** Nothing in `flows/`, `webhooks/`, `telephony/` or
`integrations/crm/` was edited at all.
"""

from app.integrations.openphone.models import OpenPhoneMirrorRow

__all__ = ["OpenPhoneMirrorRow"]
