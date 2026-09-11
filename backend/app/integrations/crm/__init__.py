"""CRM link — Phase 1, VOICE.

A self-contained, opt-in integration between OWEN and the `ghl-clone` CRM running as
`ghl_clone_api` on the same host. Everything it does is off unless BOTH of these are true:

    CRM_LINK_ENABLED=true                       (the global kill switch, default false)
    a `crm_links` row for the DID, enabled      (the per-number opt-in, default false)

With either off, the platform behaves exactly as it did before this module existed. That is
not an aspiration — it is what `tests/test_crm_link_isolation.py` asserts.

## What is in here

| module        | what it is                                                              |
|---------------|-------------------------------------------------------------------------|
| `config.py`   | PURE kernel: kill switch, destination allowlist, phone normalisation      |
| `models.py`   | the `crm_links` table — which DID is bound, and what it rings            |
| `binding.py`  | dialed DID -> binding, or None ("behave as before")                      |
| `ring.py`     | **the hybrid ring group**: softphones + up to 2 PSTN numbers, first wins |
| `softphone.py`| PURE: which CRM users may register a browser softphone, and for how long  |
| `handler.py`  | the bound-DID call: consent -> ring -> bridge+record -> else voicemail   |
| `hook.py`     | the ONE function `flows/runtime.py` calls; returns False on any failure  |
| `events.py`   | PURE: OWEN call lifecycle -> the CRM's verified `/api/events` body       |
| `push.py`     | queues events onto the EXISTING `crm_report` job (retry/backoff for free)|
| `client.py`   | HTTP to the CRM: contact resolution + event delivery (app container only)|
| `api.py`      | `/api/crm-link/*`: the delivery hop, and the CRM's outbound call/SMS API |
| `manage.py`   | `python -m app.integrations.crm.manage` — bind/unbind/list, no deploy    |

## Files OUTSIDE this package that were touched, and why

  * `flows/runtime.py` — three lines inside the existing `if not assigned:` branch. A no-op
    when the kill switch is off. This is the only call-path change.
  * `main.py` — one `include_router`. Every route 503s while disabled.
  * `models/__init__.py` — imports `CrmLink` so `alembic/env.py` sees the table. Without it
    the next `--autogenerate` would emit `DROP TABLE crm_links`.
  * `core/apikeys.py` — one new scope, `crm_link`.
  * `core/config.py` — the new `CRM_LINK_*` settings, all defaulting to off/empty
    (including `CRM_LINK_SOFTPHONE_OPERATORS`, whose empty default grants nobody a
    softphone).
  * `api/ai/deps.py` — `/api/crm-link` added to the audited path prefixes.

Each is justified in full in `.qa/state/crmlink-done`.
"""

from app.integrations.crm.models import CrmLink

__all__ = ["CrmLink"]
