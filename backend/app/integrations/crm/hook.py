"""The ONE function the existing call path calls into this module.

`flows/runtime.py::run_flow_for_stasis` gains exactly three lines:

    if await crm_hook.handle_bound_inbound(ari, channel_id, lid, str(dialed), caller_number):
        return
    await _handle_unassigned(ari, channel_id, lid, str(dialed), caller_number)

placed inside the EXISTING `if not assigned:` branch — the branch that already means "this
DID has no flow, run the built-in default". That placement is the whole safety argument:

  * A DID with a flow assigned never reaches this line, so every configured call flow is
    untouched.
  * A DID with no flow and no CRM binding gets False here and falls into
    `_handle_unassigned` on the very next line, byte for byte as it does today.
  * With `CRM_LINK_ENABLED` false this returns False before touching the database, so the
    disabled system is not merely equivalent to today's — it does strictly less work.

The function is total: it catches everything, and every failure path returns False, which
means "I did not handle this call, carry on as before". There is no failure of this module
that can leave a caller unhandled.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("integrations.crm.hook")


async def handle_bound_inbound(
    ari, channel_id: str, lid: str, dialed: str, caller_number: str
) -> bool:
    """True iff the CRM link took this call. False means "not mine — carry on".

    Ordered cheapest-first so the common case (the CRM link is off, or this DID is not
    bound) costs a boolean read and one indexed query respectively.
    """
    try:
        from app.integrations.crm import config as crm_config

        if not crm_config.link_enabled():
            return False

        from app.db import SessionLocal
        from app.integrations.crm import binding as crm_binding

        async with SessionLocal() as db:
            bound = await crm_binding.resolve(db, dialed)
        if bound is None:
            return False

        from app.integrations.crm.handler import handle_bound_inbound as run
    except Exception:  # noqa: BLE001 - THE most important except in this module.
        # Anything at all going wrong here — a bad import, a dead database, a typo in a
        # config value — must hand the call back to the handler that already works rather
        # than drop it. False is the safe answer and is always available.
        logger.exception(
            "crm-link: hook failed for DID %s (linkedid=%s); falling back to default handling",
            dialed, lid,
        )
        return False

    # PAST THE POINT OF NO RETURN. From here the CRM link owns this call, and the answer is
    # True whatever happens — `handler.handle_bound_inbound` absorbs its own failures and
    # always leaves the channel answered-and-handled or hung up. Returning False after it
    # had already answered the caller would run `_handle_unassigned` on the SAME channel,
    # playing a consent notice and a voicemail greeting over a call that is already in
    # progress. A caught exception here can only mean the handler itself was unreachable.
    logger.info("crm-link: handling DID %s (linkedid=%s, link=%s)", dialed, lid, bound.link_id)
    try:
        await run(ari, channel_id, lid, dialed, caller_number, bound)
    except Exception:  # noqa: BLE001
        logger.exception("crm-link: handler raised for DID %s (linkedid=%s)", dialed, lid)
        try:
            await ari.hangup(channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("crm-link: post-failure hangup failed (linkedid=%s)", lid)
    return True
