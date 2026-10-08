"""Retell custom functions — what the agent may ask OWEN to do mid-call (decision 13).

Retell POSTs `{"name", "call": {"call_id", ...}, "args": {...}}` to
`/api/retell/functions/<name>` (signature already verified by the route) and reads the JSON
answer back to the model. Every answer is `{"result": "<a short sentence>"}` — something the
agent can say or act on — and is produced within about a second: nothing here waits on the
call itself.

Four functions, and the rules that make them safe to expose:

  * **The pinned version decides.** A function runs only if the call's PINNED agent version
    toggles that tool on (`tools.<name>: true`). Retell's dashboard declaring a function is
    not permission; OWEN's agent version is (RETELL-PLAN C1).
  * **transfer** `{target}` — a NAME from the version's `transfer_targets` allowlist, never a
    number (flows/transfer.py says why: an LLM that can dial arbitrary numbers is a toll-fraud
    primitive). Allowed -> the worker's wait loop ends the Retell leg and the runtime moves
    the caller through `_do_agent_transfer`, the same path an owen_voice transfer takes.
    Refused -> nothing happens and the agent is told so. First request wins. ONE transfer per
    call: once a transfer on this call was tried and nobody answered, the agent has the caller
    back (flows/runtime.py `_return_to_agent`) and a second `transfer` is refused with
    "take a message instead" (2026-10-08) — never a loop of rings.
  * **end_call** — optional: Retell's native end-call works too (its leg hangs up). Both end
    the agent's part with the `end_call` port.
  * **capture_lead** — merged into the call's capture; stored as `call_captures` when the
    call ends, exactly as owen_voice's capture is.
  * **request_change** `{kind, request}` — posted to the CRM's `/api/agent-requests` (C3),
    which makes an urgent task or a Dispatch item. Nothing is moved or cancelled by it, and
    nothing that texts is queued.
"""

from __future__ import annotations

import logging

from app.flows.transfer import resolve_transfer_target, target_names

logger = logging.getLogger("integrations.retell.functions")

FUNCTIONS = ("transfer", "end_call", "capture_lead", "request_change")
REQUEST_KINDS = ("reschedule", "cancel", "other")
MAX_REQUEST_CHARS = 1000

SAY_CALL_OVER = "This call has already ended."
SAY_NOT_ALLOWED = "That is not something I can do on this line."
SAY_TRANSFERRING = "Transferring the caller now."
SAY_TRANSFER_TRIED = ("Nobody could pick up just now, so do not transfer again. Take a message "
                      "instead: ask what they need and capture it, then tell them the office will "
                      "call them back.")
SAY_ENDING = "Ending the call now."
SAY_CAPTURED = "Saved."
SAY_REQUEST_LOGGED = ("Passed to the office as an urgent request; someone will follow up with "
                      "the caller.")
SAY_REQUEST_NOT_LOGGED = ("The request could not be passed to the office automatically. Tell "
                          "the caller someone will call them back, and offer a transfer.")


def say(sentence: str, **extra) -> dict:
    return {"result": sentence, **extra}


async def _version_config(agent_version_id) -> dict:
    """The pinned version's config. Separate so tests can supply one without a database."""
    if not agent_version_id:
        return {}
    import uuid as _uuid

    from app.db import SessionLocal
    from app.models import AgentVersion

    async with SessionLocal() as db:
        row = await db.get(AgentVersion, _uuid.UUID(str(agent_version_id)))
        return dict(row.config or {}) if row is not None else {}


async def _owen_call_id(linkedid: str) -> str:
    """`calls.id` for the CRM's idempotency key, else the linkedid."""
    try:
        from app.db import SessionLocal
        from app.integrations.crm import push as crm_push

        async with SessionLocal() as db:
            owen_call_id, _rec, _trans = await crm_push.call_artifacts(db, linkedid)
        return owen_call_id or linkedid
    except Exception:  # noqa: BLE001 - the linkedid is a usable key on its own
        logger.exception("retell: calls.id lookup failed for %s", linkedid)
        return linkedid


async def _post_request(body: dict):
    """POST C3 through the existing crm-link client and the events token. Returns the
    CrmResult, or None when the link is off."""
    from app.core.config import settings
    from app.integrations.crm import config as crm_config
    from app.integrations.crm.client import CrmClient

    link = crm_config.current()
    refusal = link.delivery_refusal() or ("" if link.base_url else "no CRM_LINK_BASE_URL")
    if refusal:
        logger.warning("retell: request_change not delivered — %s", refusal)
        return None
    budget = float(getattr(settings, "CRM_LINK_HTTP_TIMEOUT_SECONDS", 5.0) or 5.0)
    return await CrmClient(link.base_url, link.token, timeout_s=budget,
                           budget_s=budget).post_agent_request(body)


async def _transfer_tried(reg, row: dict, call_id: str) -> bool:
    """Has an EARLIER Retell session on this same call already asked for a transfer? Then this
    session is the agent taking the caller back after nobody answered. Read from the registry
    (the worker's facts, not anything Retell sends), and a lookup that fails refuses: a
    second ring the caller sits through is worse than a message taken."""
    linkedid = str(row.get("linkedid") or "")
    if not linkedid:
        return False
    try:
        return bool(await reg.transfer_tried(linkedid, exclude_call_id=call_id))
    except Exception:  # noqa: BLE001 - see the docstring: refuse rather than loop
        logger.exception("retell: transfer history lookup failed (linkedid=%s)", linkedid)
        return True


async def handle(name: str, body: dict, reg) -> dict:
    """Run one function call. Never raises; the answer is always a sentence."""
    call = body.get("call") if isinstance(body.get("call"), dict) else {}
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    call_id = str(call.get("call_id") or "")
    row = await reg.get(call_id) if call_id else None
    if row is None:
        logger.warning("retell: function %s for an unknown call", name)
        return say(SAY_CALL_OVER)
    if row.get("status") != "live":
        return say(SAY_CALL_OVER)

    config = await _version_config(row.get("agent_version_id"))
    tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    if not tools.get(name):
        logger.warning("retell: function %s refused — not toggled on in the pinned version "
                       "(linkedid=%s)", name, row.get("linkedid"))
        return say(SAY_NOT_ALLOWED)

    if name == "transfer":
        if await _transfer_tried(reg, row, call_id):
            logger.info("retell: transfer refused — this call already tried one and nobody "
                        "answered (linkedid=%s)", row.get("linkedid"))
            return say(SAY_TRANSFER_TRIED)
        targets = config.get("transfer_targets")
        wanted = str(args.get("target") or args.get("destination") or "").strip()
        chosen = resolve_transfer_target(targets, wanted)
        if chosen is None:
            logger.warning("retell: transfer to %r refused — not on the allowlist "
                           "(linkedid=%s)", wanted, row.get("linkedid"))
            names = target_names(targets)
            return say(SAY_NOT_ALLOWED + (" You can transfer to: " + ", ".join(names) + "."
                                          if names else ""))
        await reg.request_exit(call_id, "transfer", {"destination": chosen["name"]})
        return say(SAY_TRANSFERRING)

    if name == "end_call":
        await reg.request_exit(call_id, "end_call", {})
        return say(SAY_ENDING)

    if name == "capture_lead":
        fields = {str(k): v for k, v in args.items()
                  if isinstance(v, (str, int, float, bool)) and str(v).strip()}
        if fields:
            await reg.merge_capture(call_id, fields)
        return say(SAY_CAPTURED)

    if name == "request_change":
        kind = str(args.get("kind") or "other").strip().lower()
        if kind not in REQUEST_KINDS:
            kind = "other"
        text = " ".join(str(args.get("request") or "").split())[:MAX_REQUEST_CHARS]
        if not text:
            return say("Say what the caller wants changed, in their words, then call this "
                       "again.")
        payload = {
            "caller_number": str(row.get("caller_number") or ""),
            "agent_name": str(row.get("agent_name") or ""),
            "owen_call_id": await _owen_call_id(str(row.get("linkedid") or "")),
            "kind": kind,
            "request": text,
        }
        created, where = False, None
        try:
            result = await _post_request(payload)
            if result is not None and result.ok:
                created = bool((result.data or {}).get("created"))
                where = (result.data or {}).get("where")
        except Exception:  # noqa: BLE001 - the caller is still on the line
            logger.exception("retell: request_change delivery failed")
        try:
            await reg.add_request(call_id, {"kind": kind, "request": text,
                                            "created": created, "where": where})
        except Exception:  # noqa: BLE001
            logger.exception("retell: storing request_change failed")
        return say(SAY_REQUEST_LOGGED if created else SAY_REQUEST_NOT_LOGGED,
                   created=created)

    return say(SAY_NOT_ALLOWED)
