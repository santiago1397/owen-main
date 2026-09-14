"""`GET /api/link-status` — is the CRM's side of this platform working? READ-ONLY.

The CRM (`ghl-clone`) draws one small status dot in its top bar on every page. The owner:
"it should always show if we are online or not so i know if everything working good". Two
of its three checks are about THIS platform, and this route is where the CRM's backend asks:

  * **the link** — is `CRM_LINK_ENABLED` on, and is telephony (`ASTERISK_ENABLED`) on? That
    this route answered at all is the "the CRM can reach owen-main" half, measured on the
    CRM side.
  * **Quo sync** — is the OpenPhone mirror on and keyed, when did its poll last tick, has the
    30-day backfill completed, is the Quo webhook on, and when did the last one arrive?

(The third check, the browser's own SIP registration, is already live in the browser.)

## Why a route of its own

`/api/crm-link/health` exists, but it is the wrong shape for a dot polled all day: it 503s
while the kill switch is off (the one state the dot most needs to report), it probes the CRM
back over HTTP on every call, and it lists the bound numbers and PSTN ring destinations. The
two routers it could have joined are FENCED at their exact route lists
(`test_crm_softphone_creds`, `test_openphone_mirror`), and the fence counts every path that
starts with `/api/crm-link` — hence `/api/link-status`, not `/api/crm-link/status`.

## What it will and will not do

  * **GET only, and it writes nothing.** Three reads: two `app_settings` rows and the time
    of the newest webhook-sourced `crm_report` job. No OpenPhone request, no CRM request, no
    Asterisk request. `test_link_status.py` drives it against a session that fails the test
    on any `add`/`commit`/`delete`.
  * **Authenticated like every CRM-facing route**: an API key with the `crm_link` scope, via
    `X-OWEN-Key`, the same key the CRM already holds for `/api/softphone/credentials`.
  * **No kill-switch 503.** Reporting that a switch is off is this route's job.
  * **Names nobody.** No phone number (not even ours), no URL, no token, no message text.
    The mirror's include/exclude lists are deliberately absent.

## "Last webhook delivery", without a migration

A verified, handled Quo webhook writes exactly one `crm_report` job whose URL is
`webhook.PROCESS_PATH` (`webhook.accept`). The newest such job's `created_at` IS the time
the last accepted delivery arrived, so nothing new has to be stored. It is "accepted", not
"received": an event type we ignore, or one refused for a bad signature, enqueues nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import require_scope
from app.core.apikeys import SCOPE_CRM_LINK
from app.core.config import settings
from app.db import get_db
from app.integrations.crm import config as crm_config
from app.integrations.openphone import config as op_config
from app.integrations.openphone import push as op_push
from app.integrations.openphone.models import BACKFILL_SETTING_KEY, LAST_TICK_SETTING_KEY
from app.integrations.openphone.webhook import PROCESS_PATH
from app.models import AppSetting, Job

logger = logging.getLogger("integrations.link_status")

router = APIRouter(prefix="/api/link-status", tags=["link-status"])


def _iso(value: Any) -> Optional[str]:
    """An ISO-8601 string, or None. Accepts a datetime or a string already in that form."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def build_status(*, crm: crm_config.CrmLinkSettings, mirror: op_config.MirrorSettings,
                 telephony_enabled: bool, webhook_enabled: bool,
                 webhook_secret_configured: bool, last_tick: Optional[dict],
                 backfill: Optional[dict], last_webhook_at: Any,
                 database_readable: bool, now: datetime) -> dict:
    """The response body. PURE: every fact is passed in, so a test can build any state.

    Only facts, no verdicts. The CRM decides what is green, amber or red, because it also
    knows the one thing this side cannot: whether it reached us at all.
    """
    tick = last_tick if isinstance(last_tick, dict) else {}
    done = backfill if isinstance(backfill, dict) else {}
    return {
        "checked_at": _iso(now),
        "database_readable": bool(database_readable),
        "crm_link": {
            "enabled": bool(crm.enabled),
            "telephony_enabled": bool(telephony_enabled),
        },
        "quo": {
            "mirror_enabled": bool(mirror.enabled),
            "api_key_present": bool(mirror.api_key_present),
            "poll_seconds": int(mirror.poll_seconds),
            "last_tick_at": _iso(tick.get("at")),
            "last_tick_ran": bool(tick["ran"]) if "ran" in tick else None,
            "last_tick_reason": tick.get("reason") if tick.get("reason") else None,
            "backfill_days": int(mirror.backfill_days),
            "backfill_completed_at": _iso(done.get("completed_at")),
            "webhook_enabled": bool(webhook_enabled),
            "webhook_secret_configured": bool(webhook_secret_configured),
            "last_webhook_at": _iso(last_webhook_at),
        },
    }


async def read_setting(db: AsyncSession, key: str) -> Optional[dict]:
    row = await db.get(AppSetting, key)
    value = getattr(row, "value", None)
    return value if isinstance(value, dict) else None


async def read_last_webhook_at(db: AsyncSession):
    """`created_at` of the newest job a verified Quo webhook enqueued, or None."""
    return (await db.execute(
        select(func.max(Job.created_at)).where(
            Job.type == op_push.JOB_TYPE,
            Job.payload["url"].astext.like("%" + PROCESS_PATH),
        )
    )).scalar()


@router.get("")
async def link_status(
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> dict:
    """What the CRM's status dot needs to know. See the module docstring."""
    last_tick = backfill = last_webhook_at = None
    readable = True
    try:
        last_tick = await read_setting(db, LAST_TICK_SETTING_KEY)
        backfill = await read_setting(db, BACKFILL_SETTING_KEY)
        last_webhook_at = await read_last_webhook_at(db)
    except Exception as exc:  # noqa: BLE001 - the status route must still answer
        # A database that cannot be read is itself a status, and the CRM shows it. Raising
        # would turn "the mirror state is unknown" into "the phone system is down".
        readable = False
        logger.warning("link-status: could not read the mirror state (%s)",
                       type(exc).__name__)
    return build_status(
        crm=crm_config.current(),
        mirror=op_config.current(),
        telephony_enabled=bool(settings.ASTERISK_ENABLED),
        webhook_enabled=bool(settings.OPENPHONE_WEBHOOK_ENABLED),
        webhook_secret_configured=bool(str(settings.OPENPHONE_WEBHOOK_SECRET or "").strip()),
        last_tick=last_tick,
        backfill=backfill,
        last_webhook_at=last_webhook_at,
        database_readable=readable,
        now=datetime.now(timezone.utc),
    )
