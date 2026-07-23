"""Manual operator OUTBOUND calling — soft guardrails + defensive opt-out resolution (Ticket 14).

PURE (stdlib only) so it is unit-testable without fastapi/sqlalchemy (like credentials.py /
control.py). The FastAPI endpoint in app/api/telephony.py composes these with the DB (owned-DID
lookup, opt-out query) and the ARI orchestration in app/telephony/control.place_outbound_call.

Locked design (Ticket 14):
- Guardrails are SOFT + NON-BLOCKING: they only produce warning strings the operator may ignore.
  There is NO hard DNC / calling-hours block.
- Time window: warn when the callee's LOCAL time is outside 8am–9pm. The callee's timezone is
  approximated from the NANP area code (a soft, best-effort signal — DST is NOT modelled; a
  warning is advisory, never a block), defaulting to Eastern when unknown.
- Opt-out: warn on an `sms_opt_outs` hit, but ONLY if that table/model exists (it is added by a
  concurrent Ticket 10 and may not be present yet). `resolve_opt_out_model()` returns None when
  it isn't there, so the caller skips the check silently.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

# Soft calling-window (callee local time): 8am inclusive .. 9pm exclusive.
WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 21  # 9pm

# NANP area code -> standard UTC offset (hours). Best-effort ONLY: this is a soft advisory
# signal, so a compact representative map is deliberate — DST is not applied and unknown area
# codes fall back to Eastern (the business default). Extend freely; correctness is non-critical.
_AREA_CODE_OFFSET = {
    # Eastern (-5)
    "212": -5, "305": -5, "404": -5, "617": -5, "202": -5, "786": -5, "954": -5, "561": -5,
    "407": -5, "813": -5, "716": -5, "215": -5, "412": -5, "313": -5, "216": -5,
    # Central (-6)
    "312": -6, "713": -6, "214": -6, "469": -6, "512": -6, "615": -6, "504": -6, "816": -6,
    "210": -6, "281": -6, "832": -6, "402": -6,
    # Mountain (-7)
    "303": -7, "720": -7, "505": -7, "801": -7, "602": -7, "480": -7, "970": -7,
    # Pacific (-8)
    "213": -8, "310": -8, "323": -8, "415": -8, "510": -8, "619": -8, "206": -8, "503": -8,
    "702": -8, "408": -8, "858": -8, "925": -8,
    # Alaska (-9) / Hawaii (-10)
    "907": -9, "808": -10,
}

# Fallback when the area code is unknown/foreign — the business timezone is Eastern.
DEFAULT_UTC_OFFSET = -5


def _digits(number: str) -> str:
    return re.sub(r"\D", "", str(number or ""))


def area_code(number: str) -> Optional[str]:
    """The 3-digit NANP area code of an E.164/US number, or None if it can't be read.
    Handles a leading country code '1' (11-digit) and a bare 10-digit number."""
    d = _digits(number)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) >= 10:
        return d[:3]
    return None


def utc_offset_for_number(number: str) -> int:
    """Best-effort standard UTC offset (hours) for the callee, from the area code."""
    ac = area_code(number)
    return _AREA_CODE_OFFSET.get(ac, DEFAULT_UTC_OFFSET) if ac else DEFAULT_UTC_OFFSET


def local_hour(number: str, now_utc: datetime) -> int:
    """The callee's approximate LOCAL hour (0–23) for `now_utc` (offset-aware or naive-UTC)."""
    return (now_utc.hour + utc_offset_for_number(number)) % 24


def time_window_warning(number: str, now_utc: datetime) -> Optional[str]:
    """Soft warning string when the callee's local time is outside 8am–9pm, else None.
    Non-blocking — the operator may proceed regardless."""
    hour = local_hour(number, now_utc)
    if hour < WINDOW_START_HOUR or hour >= WINDOW_END_HOUR:
        return (
            f"Outside 8am–9pm in the callee's local time "
            f"(~{hour:02d}:00, approx from area code)."
        )
    return None


def is_owned_bulkvs_did(number_row, *, owner_provider: str) -> bool:
    """True iff a `numbers` row is a usable outbound caller-ID: owned by BulkVS, active, and not
    soft-released. PURE (duck-typed on attributes) so the from-number restriction is testable
    without a DB — the endpoint applies the SAME predicate over queried rows. Foreign/spoofed
    numbers are out of scope (locked design)."""
    return (
        getattr(number_row, "owner_provider", None) == owner_provider
        and bool(getattr(number_row, "active", False))
        and getattr(number_row, "released_at", None) is None
    )


def owned_from_number_set(number_rows, *, owner_provider: str) -> set:
    """The set of phone numbers from `number_rows` allowed as an outbound from-number."""
    return {
        r.phone_number
        for r in number_rows
        if is_owned_bulkvs_did(r, owner_provider=owner_provider)
    }


def opt_out_warning(opted_out: Optional[bool]) -> Optional[str]:
    """Soft warning when the callee is on the SMS opt-out list. `opted_out is None` (couldn't
    determine — table absent / not yet migrated) yields NO warning (skipped silently)."""
    if opted_out:
        return "This number is on the SMS opt-out list."
    return None


def resolve_opt_out_model():
    """The Ticket-10 `SmsOptOut` model if it exists, else None (concurrent ticket may not be
    merged yet). DEFENSIVE: any import error => None so the opt-out check is skipped silently."""
    try:
        import app.models as models  # noqa: WPS433 - lazy so this module stays import-light
    except Exception:  # noqa: BLE001 - sqlalchemy/model layer absent => skip opt-out silently
        return None
    return getattr(models, "SmsOptOut", None)
