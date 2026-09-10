"""Resolve a dialed DID to its CRM binding, if it has one.

This is the ONE place that answers "is this number CRM-linked?", and it is the reason the
rest of the platform is unaffected: for any number without an enabled `crm_links` row it
returns None, and every caller then does exactly what it did before this module existed.

Lookup is by (phone_number, media_provider) — the same key `flows/runtime.py` uses to find
a number's flow, and for the same reason recorded on the `Number` model: a BulkVS DID is
OWNED by the 'bulkvs' provider row but carries its MEDIA on 'asterisk', so keying on the
call's `provider_id` finds nothing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from app.core.config import settings
from app.integrations.crm import config as crm_config
from app.integrations.crm.models import CrmLink
from app.models import Number

logger = logging.getLogger("integrations.crm.binding")


@dataclass(frozen=True)
class CrmBinding:
    """A resolved binding: the DB row flattened, with env defaults already applied.

    Frozen and plain so it can cross out of the database session it was loaded in — the
    call it configures may then ring, bridge and record for the next forty minutes, and
    holding a session open for that is the mistake `flows/runtime.py` documents avoiding.
    """

    link_id: str
    number_id: str
    phone_number: str
    friendly_name: Optional[str]
    campaign_id: Optional[str]
    ring_operators: bool
    operator_ids: list[str] = field(default_factory=list)
    pstn_numbers: list[str] = field(default_factory=list)
    ring_timeout_seconds: int = 25
    crm_base_url: str = ""
    crm_token: str = ""
    outbound_operator: Optional[str] = None

    def delivery_refusal(self) -> Optional[str]:
        """Why this binding cannot push events right now, or None."""
        if not self.crm_base_url:
            return "no CRM base URL configured for this binding"
        if not self.crm_token:
            return crm_config.REFUSE_NO_TOKEN
        return None


def _token_for(row: CrmLink, cfg: crm_config.CrmLinkSettings) -> str:
    """The machine token for this binding.

    `crm_token_env` names an environment variable and is read from the process environment
    at resolve time. The row never holds the secret, so `crm_links` stays safe to dump, to
    read over `/api/ai/query`, and to paste into a support thread.
    """
    env_name = (row.crm_token_env or "").strip()
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            logger.warning(
                "crm-link: binding %s names token env %r but it is unset", row.id, env_name
            )
        return value
    return cfg.token


def _string_list(value) -> list[str]:
    """A JSONB column that should hold a list of strings, defensively.

    A hand-edited row is a real possibility — this table is meant to be operated by a human
    — so a scalar, a null or a list with junk in it must degrade to something sane rather
    than raise inside a live call's routing decision.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


async def resolve(db, dialed_number: str) -> Optional[CrmBinding]:
    """The binding for a dialed DID, or None.

    None — meaning "behave exactly as you did before" — is returned for every one of:
      * the global kill switch is off;
      * no `numbers` row for that DID on the Asterisk media provider;
      * no `crm_links` row for it;
      * the row exists but `enabled` is false;
      * anything at all went wrong.

    That last clause is deliberate. This runs on the call path of a live phone system, and
    the correct response to a database hiccup here is the behaviour that already works, not
    a new one.
    """
    if not crm_config.link_enabled():
        return None
    dialed = str(dialed_number or "").strip()
    if not dialed:
        return None

    try:
        row = (
            await db.execute(
                select(CrmLink, Number)
                .join(Number, Number.id == CrmLink.number_id)
                .where(
                    Number.phone_number == dialed,
                    Number.media_provider == settings.BULKVS_MEDIA_PROVIDER,
                    CrmLink.enabled.is_(True),
                )
                .limit(1)
            )
        ).first()
    except Exception:  # noqa: BLE001 - an unreadable binding must never change call handling
        logger.exception("crm-link: binding lookup failed for DID %s", dialed)
        return None
    if row is None:
        return None

    link, number = row
    cfg = crm_config.settings_view(settings)
    return CrmBinding(
        link_id=str(link.id),
        number_id=str(number.id),
        phone_number=number.phone_number,
        friendly_name=number.friendly_name,
        campaign_id=str(number.campaign_id) if number.campaign_id else None,
        ring_operators=bool(link.ring_operators),
        operator_ids=_string_list(link.operator_ids),
        pstn_numbers=_string_list(link.pstn_numbers),
        ring_timeout_seconds=int(link.ring_timeout_seconds or cfg.ring_timeout_seconds or 25),
        crm_base_url=str(link.crm_base_url or cfg.base_url or "").rstrip("/"),
        crm_token=_token_for(link, cfg),
        outbound_operator=(link.outbound_operator or None),
    )


async def resolve_for_number_id(db, number_id) -> Optional[CrmBinding]:
    """The binding for a `numbers.id`. Used by the CRM-facing outbound/SMS endpoints, which
    are given a from-number rather than a dialed one."""
    if not crm_config.link_enabled():
        return None
    try:
        number = (
            await db.execute(select(Number).where(Number.id == number_id).limit(1))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception("crm-link: number lookup failed for %s", number_id)
        return None
    if number is None:
        return None
    return await resolve(db, number.phone_number)
