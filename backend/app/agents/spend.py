"""The daily AI spend cap and its alert — settable at runtime (RETELL-PLAN C6, decision 11).

`AI_DAILY_SPEND_CAP_USD` (AI_AGENT_SPEC D14) has been an env-only switch: changing it meant a
redeploy. The CRM now owns the number ("Spend cap is a setting, pilot $25/day, alert at 80%"),
so it is stored in `app_settings` under "agent_spend" and that value WINS over the env. The env
values are only the defaults for a deployment the CRM has never configured — which keeps the
behaviour of every existing deployment exactly what it was until somebody sets a cap.

## What is counted

Every `call_charges` row whose kind is in `ai_cost.AI_KINDS`, over the last 24 hours — the
window `remote._over_spend_cap` has always used, kept so the cap means the same thing for
both engines. owen_voice writes DERIVED rows (usage x list price); Retell writes ONE row per
call carrying Retell's own `call_cost.combined_cost` (`KIND_AI_RETELL`). `today_usd` in the
CRM's answer is this same figure, so the number the owner reads is the number that is enforced.

## Fail OPEN, on purpose

A cap check that errors lets the call through. A cost guard that can send every caller to
voicemail when the database hiccups is worse than the overspend it prevents — the cap is a
backstop against a runaway loop, not a billing system. Same rule as before this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

logger = logging.getLogger("agents.spend")

SETTING_KEY = "agent_spend"
# The day the alert last fired, so it is said once a day rather than on every call after.
ALERTED_KEY = "agent_spend_alerted"

ALERT_PCT_MIN = 1
ALERT_PCT_MAX = 100


def effective_limits(stored: dict | None, *, env_cap: float, env_alert_pct: int) -> tuple[float, int]:
    """`(daily_cap_usd, alert_pct)`: the stored setting where it names a value, else the env
    default. PURE. A stored value that is not a number is ignored rather than trusted — a bad
    row must not turn the cap off or into nonsense."""
    cap, pct = float(env_cap or 0), int(env_alert_pct or 80)
    if isinstance(stored, dict):
        try:
            if stored.get("daily_cap_usd") is not None:
                cap = max(0.0, float(stored["daily_cap_usd"]))
        except (TypeError, ValueError):
            pass
        try:
            if stored.get("alert_pct") is not None:
                pct = int(stored["alert_pct"])
        except (TypeError, ValueError):
            pass
    pct = min(ALERT_PCT_MAX, max(ALERT_PCT_MIN, pct))
    return cap, pct


def validate_limits(daily_cap_usd, alert_pct) -> list[str]:
    """Why a PUT is refused, as sentences. PURE. `0` is a legitimate cap: it means "no cap",
    exactly as the env setting has always read it."""
    problems: list[str] = []
    try:
        cap = float(daily_cap_usd)
        if cap < 0 or cap != cap:  # NaN
            problems.append("daily_cap_usd must be zero (no cap) or a positive amount")
        elif cap > 10000:
            problems.append("daily_cap_usd over $10,000 a day is refused; that is not a cap")
    except (TypeError, ValueError):
        problems.append("daily_cap_usd must be a number of dollars")
    try:
        pct = int(alert_pct)
        if str(alert_pct).strip() != str(pct) and not isinstance(alert_pct, int):
            raise ValueError
        if not ALERT_PCT_MIN <= pct <= ALERT_PCT_MAX:
            problems.append(f"alert_pct must be between {ALERT_PCT_MIN} and {ALERT_PCT_MAX}")
    except (TypeError, ValueError):
        problems.append("alert_pct must be a whole number of percent")
    return problems


def should_alert(spent: float, cap: float, alert_pct: int) -> bool:
    """True once spend reaches `alert_pct` percent of a non-zero cap. PURE."""
    return cap > 0 and spent >= cap * (alert_pct / 100.0)


async def limits(db) -> tuple[float, int]:
    """The cap and alert percent in force now."""
    from app.core.config import settings
    from app.models import AppSetting

    row = await db.get(AppSetting, SETTING_KEY)
    return effective_limits(
        row.value if row is not None else None,
        env_cap=float(getattr(settings, "AI_DAILY_SPEND_CAP_USD", 0) or 0),
        env_alert_pct=int(getattr(settings, "AI_SPEND_ALERT_PCT", 80) or 80),
    )


async def spent_today(db) -> Decimal:
    """AI spend over the last 24 hours, in dollars. See the module docstring for the window."""
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from app.models import CallCharge
    from app.services.ai_cost import AI_KINDS

    since = datetime.now(timezone.utc) - timedelta(days=1)
    spent = (await db.execute(
        select(sa_func.coalesce(sa_func.sum(CallCharge.amount), 0)).where(
            CallCharge.kind.in_(AI_KINDS),
            CallCharge.created_at >= since,
        )
    )).scalar_one()
    return Decimal(str(spent or 0))


async def set_limits(db, *, daily_cap_usd: float, alert_pct: int) -> None:
    """Store the CRM's setting (C6). The caller commits. One writer (the CRM's ADMIN), so a
    read-modify-write on the key is enough; the alert day is reset so a raised cap can warn
    again the same day."""
    from app.models import AppSetting

    value = {"daily_cap_usd": float(daily_cap_usd), "alert_pct": int(alert_pct)}
    row = await db.get(AppSetting, SETTING_KEY)
    if row is None:
        db.add(AppSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    alerted = await db.get(AppSetting, ALERTED_KEY)
    if alerted is not None:
        await db.delete(alerted)


async def over_cap() -> bool:
    """True when today's AI spend has hit the cap in force. Fails OPEN (see the docstring)."""
    try:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            cap, _pct = await limits(db)
            if cap <= 0:
                return False
            return float(await spent_today(db)) >= cap
    except Exception:  # noqa: BLE001 - fail open; see the module docstring
        logger.debug("spend cap check unavailable", exc_info=True)
        return False


async def check_alert() -> None:
    """Log ONE warning a day once spend crosses the alert percent. Best-effort; never raises.

    A WARNING because that is what lands in /api/ai/errors (core/logcapture), which is where
    an operator already looks when agents misbehave. It names dollars, never a caller.
    """
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.db import SessionLocal
        from app.models import AppSetting

        async with SessionLocal() as db:
            cap, pct = await limits(db)
            spent = float(await spent_today(db))
            if not should_alert(spent, cap, pct):
                return
            today = datetime.now(timezone.utc).date().isoformat()
            row = await db.get(AppSetting, ALERTED_KEY)
            if row is not None and (row.value or {}).get("day") == today:
                return
            await db.execute(
                pg_insert(AppSetting).values(key=ALERTED_KEY, value={"day": today})
                .on_conflict_do_update(index_elements=["key"], set_={"value": {"day": today}})
            )
            await db.commit()
        logger.warning(
            "AI spend alert: $%.2f of the $%.2f daily cap spent (alert at %d%%). New agent "
            "calls go to the flow's fallback (voicemail) once the cap is reached.",
            spent, cap, pct,
        )
    except Exception:  # noqa: BLE001 - an alert must never affect a call or a webhook
        logger.debug("spend alert check unavailable", exc_info=True)
