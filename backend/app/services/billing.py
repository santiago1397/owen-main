"""Pure BulkVS cost kernel (Billing tab).

USAGE COST IS NOT ESTIMATED. BulkVS exposes `GET /voice` — its own RATED call detail
records, carrying `perMinute` (the rate they applied) and `amount` (what they actually
charged). That feed is the source of truth for per-call cost, so no local rate table is
consulted when pricing a call.

(An earlier design costed usage from Asterisk's CDR against a hand-maintained rate table,
on the mistaken belief that BulkVS had no billing API. Probing guessed endpoint names
(/cdr, /usage, /invoice, /billing) is what produced that error — they all return the same
200-with-1-byte body as a nonsense URL, but the OpenAPI spec at /api/v1.0/openapi lists
/voice. Checking real records against that approach found it wrong in BOTH directions:
Asterisk reports billsec=0 on a flow-dial leg that BulkVS actually billed for 21 seconds,
and outbound is not a flat $0.004 — one observed call rated at $0.0099/min.)

The RATE TABLE survives for what /voice does not cover: recurring per-DID monthly charges,
E911, and one-time setup fees. Those are account-level facts with no call record behind them.

THE CENTRAL FACT, confirmed against live BulkVS records: one caller-facing call can be TWO
billed records. An inbound call that a flow forwards back out over the same trunk bills as
inbound minutes AND outbound minutes, seconds apart:
    15:04:41  inbound   +12178584185 -> 15618788090   16s  $0.00009
    15:04:49  outbound  +12178584185 -> 19549147244    7s  $0.00080
`calls` deliberately collapses every leg onto one row (provider_call_sid = Linkedid), which
is exactly the wrong shape for billing — hence a per-LEG projection.

Kept stdlib-only (no sqlalchemy, no app.core.config) so record parsing, rounding and the
recurring-rate lookups are unit-testable in a bare sandbox, exactly like
app.flows.interpreter and app.services.inbox_threads. The DB-aware applier lives in
app/workers/billing.py.

RATE PROVENANCE is tracked per rate row (`source`), because not every number is equally
trustworthy:
  - "sheet" — read off the operator's own BulkVS portal price table. Authoritative.
  - "web"   — filled from public BulkVS pricing where the portal sheet had a "View" link.
Both are now only used for RECURRING charges; the /voice feed supersedes them for usage.
The published per-minute rows are kept for reference (and their LNP fees), and live records
confirm them: observed inbound rated at exactly $0.0003/min, outbound at $0.004/min.

A record whose amount cannot be read is marked `unrated` and surfaced, NEVER counted as $0
— a bill that quietly under-reports is worse than one that admits ignorance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# --- rate codes -------------------------------------------------------------------------

# Per-minute usage
INBOUND_TIER_PREFIX = "inbound.tier."      # + BulkVS TN Details.Tier verbatim ("0", "AK", …)
INBOUND_TOLLFREE = "inbound.tollfree"
OUTBOUND_DOMESTIC = "outbound.domestic"

# Per-event
CNAM_DIP = "cnam.dip"
LRN_DIP = "lrn.dip"
DID_SETUP = "did.setup"
SMS_OUTBOUND = "sms.outbound"
SMS_INBOUND = "sms.inbound"
SMS_ENABLEMENT = "sms.enablement"

# Per-month
DID_MONTHLY_PREFIX = "did.monthly.tier."   # + tier
TOLLFREE_MONTHLY = "did.monthly.tollfree"
E911_MONTHLY = "e911.monthly"

# Charge kinds written to call_charges.
KIND_MINUTES = "minutes"
# CNAM IS billed, at $0.002 per inbound call — but NOT inside the /voice record, which is why
# it was briefly (and wrongly) treated as free. Each inbound record's `amount` equals its
# minutes exactly (24s x $0.0003/60 = $0.00012), with the looked-up name delivered as a
# seemingly-free field. The dip is deducted from the account balance separately.
#
# It was recovered by reconciling the balance: $25.00 funded, $24.76 remaining = $0.2378
# spent, and the ONLY combination of published BulkVS charges that lands on that figure is
# voice + 1 setup + 2 monthly + 9 CNAM dips. Worth modelling despite its size — at $0.002 per
# CALL rather than per minute it was 36% of this account's voice spend, and its share grows
# as calls get shorter.
KIND_CNAM = "cnam"

# BulkVS bills in 6-second increments with a 6-second minimum. Sourced from public BulkVS
# documentation, NOT the operator's portal sheet (which states only "Rate/Min") — it is the
# one figure most worth confirming against a real invoice, because at this account's very
# short call profile (15s / 28s calls) the difference between 6s and 60s rounding is ~2.5x.
DEFAULT_INCREMENT_SECONDS = 6
DEFAULT_MINIMUM_SECONDS = 6

# NANP toll-free area codes. A toll-free DID is billed on its own rate row regardless of the
# tier BulkVS reports for it, so this check takes precedence over the tier lookup.
TOLL_FREE_NPAS = frozenset({"800", "888", "877", "866", "855", "844", "833", "822"})

# Money is stored at 6dp (a single 6-second increment of the cheapest tier is $0.00003, so
# 4dp would round real charges to zero) and displayed at 4dp.
_MONEY_Q = Decimal("0.000001")


def _d(value) -> Decimal:
    """Coerce to Decimal without going through binary float where avoidable."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value if value is not None else 0))


# --- seed rate table --------------------------------------------------------------------
# Consumed by the Alembic migration to populate `billing_rates`. `unit` is one of
# per_minute / per_event / per_month. `lnp_fee` rides along on inbound tier rows because the
# price sheet publishes it there; it is informational (a port-in is a manual adjustment).

SEED_RATES: list[dict] = [
    # --- inbound DID tiers (per-minute) + their monthly MRC ------------------------------
    # (code, label, per-minute rate, monthly, LNP fee) straight off the portal sheet.
    *[
        row
        for tier, label, per_min, monthly, lnp in [
            ("0", "DID US48 Tier 0", "0.0003", "0.06", "0.00"),
            ("10", "DID US48 Tier 10", "0.0012", "0.06", "0.00"),
            ("1", "DID Tier 1", "0.0065", "0.15", "3.00"),
            ("2", "DID Tier 2", "0.0099", "0.15", "5.00"),
            ("3", "DID Tier 3", "0.0171", "0.15", "3.00"),
            ("4", "DID Tier 4", "0.0003", "0.06", "3.00"),
            ("AK", "DID Alaska Tier AK", "0.0060", "0.25", "5.00"),
            ("PRI", "DID Puerto Rico Tier PRI", "0.0099", "0.55", "8.00"),
            ("5", "DID Canada Tier 5", "0.0198", "0.25", "5.00"),
            ("6", "DID Canada Tier 6", "0.0020", "0.25", "5.00"),
        ]
        for row in (
            {
                "code": f"{INBOUND_TIER_PREFIX}{tier}",
                "label": f"{label} — inbound",
                "unit": "per_minute",
                "amount": per_min,
                "lnp_fee": lnp,
                "source": "sheet",
            },
            {
                "code": f"{DID_MONTHLY_PREFIX}{tier}",
                "label": f"{label} — monthly",
                "unit": "per_month",
                "amount": monthly,
                "source": "sheet",
            },
        )
    ],
    # --- toll free ------------------------------------------------------------------------
    {"code": INBOUND_TOLLFREE, "label": "Toll Free US-48/Canada — inbound",
     "unit": "per_minute", "amount": "0.0055", "source": "sheet"},
    {"code": TOLLFREE_MONTHLY, "label": "Toll Free — monthly",
     "unit": "per_month", "amount": "0.14", "source": "sheet"},
    # --- outbound -------------------------------------------------------------------------
    # The sheet's Outbound section lists only "Outbound Calling Domestic". International is
    # published nowhere, so a non-NANP destination resolves to NO code and is flagged unrated.
    {"code": OUTBOUND_DOMESTIC, "label": "Outbound Calling Domestic",
     "unit": "per_minute", "amount": "0.0040", "source": "sheet"},
    # --- additional services --------------------------------------------------------------
    # Charged per INBOUND call on a CNAM-enabled DID, deducted from the balance rather than
    # included in the /voice record's amount (see KIND_CNAM).
    {"code": CNAM_DIP, "label": "CNAM lookup (per inbound call)", "unit": "per_event",
     "amount": "0.0020", "source": "sheet"},
    {"code": LRN_DIP, "label": "LRN lookup", "unit": "per_event",
     "amount": "0.0001", "source": "sheet"},
    {"code": E911_MONTHLY, "label": "E911 per number", "unit": "per_month",
     "amount": "0.49", "source": "sheet"},
    # Not on the portal sheet — from public BulkVS pricing.
    {"code": DID_SETUP, "label": "DID setup fee (one-time)", "unit": "per_event",
     "amount": "0.25", "source": "web"},
    {"code": SMS_OUTBOUND, "label": "SMS outbound", "unit": "per_event",
     "amount": "0.0060", "source": "web"},
    {"code": SMS_INBOUND, "label": "SMS inbound", "unit": "per_event",
     "amount": "0.0030", "source": "web"},
    {"code": SMS_ENABLEMENT, "label": "SMS enablement", "unit": "per_event",
     "amount": "0.0100", "source": "web"},
]


# --- helpers ------------------------------------------------------------------------------


def is_toll_free(e164: str | None) -> bool:
    digits = "".join(c for c in (e164 or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits[:3] in TOLL_FREE_NPAS


# --- rounding + costing ---------------------------------------------------------------------


def round_billsec(
    billsec: int,
    increment_seconds: int = DEFAULT_INCREMENT_SECONDS,
    minimum_seconds: int = DEFAULT_MINIMUM_SECONDS,
) -> int:
    """Answered seconds -> BILLED seconds under the carrier's increment.

    An unanswered leg (billsec 0) bills nothing — the minimum applies to connected calls
    only, never to a call that never answered. Otherwise round UP to the next increment and
    apply the minimum. Guards against a zero/negative increment (bad config) by degrading to
    per-second rather than dividing by zero.
    """
    if billsec <= 0:
        return 0
    inc = increment_seconds if increment_seconds and increment_seconds > 0 else 1
    minimum = max(minimum_seconds or 0, 0)
    rounded = ((billsec + inc - 1) // inc) * inc
    return max(rounded, minimum)


def cost_of_minutes(billed_seconds: int, rate_per_minute) -> Decimal:
    """Cost of `billed_seconds` at a per-minute rate, quantized to 6dp."""
    if billed_seconds <= 0:
        return Decimal("0").quantize(_MONEY_Q)
    minutes = _d(billed_seconds) / Decimal(60)
    return (minutes * _d(rate_per_minute)).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


def quantize_money(value) -> Decimal:
    return _d(value).quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


# --- BulkVS /voice rated records (the authoritative usage feed) ----------------------------

# Directions BulkVS reports in `callType`. e911 / 8xx / 8yy are accepted verbatim so an
# unexpected class is still recorded and costed from THEIR amount rather than dropped.
VOICE_INBOUND = "inbound"
VOICE_OUTBOUND = "outbound"

# BulkVS stamps `callStart` in the account's display timezone with NO offset. Verified
# against the same calls in Asterisk's (UTC) CDR: BulkVS 15:04:41 == 19:04 UTC, i.e. UTC-4
# (US Eastern, DST). Parsed with an explicit zone rather than assumed UTC — treating these
# as UTC would file every charge 4-5 hours late and land them in the wrong day bucket.
VOICE_DEFAULT_TZ = "America/New_York"


@dataclass
class VoiceCharge:
    """One RATED record from BulkVS /voice — what they actually charged for one leg."""

    call_ref: str                  # BulkVS callID; the idempotency key
    direction: str                 # callType verbatim: inbound | outbound | e911 | 8xx | 8yy
    started_at: object | None      # aware UTC datetime
    duration_seconds: int
    source: str | None             # callSource (caller ID)
    destination: str | None        # callDestination (called number)
    per_minute: Decimal | None     # the rate BULKVS applied — not ours
    amount: Decimal | None         # what BULKVS charged
    trunk_group: str | None = None
    cnam: str | None = None        # delivered CNAM/city-state; informational, not billed
    unrated: bool = False
    unrated_reason: str | None = None

    @property
    def billed_seconds(self) -> int:
        """Seconds BulkVS actually billed, implied by amount / perMinute.

        Derived from THEIR two numbers rather than by re-applying our own rounding, so the
        figure shown always reconciles with the charge. Falls back to our 6-second rounding
        only when the rate is zero/absent and nothing can be implied.
        """
        if self.per_minute and self.per_minute > 0 and self.amount is not None:
            return int(round(float(self.amount) / float(self.per_minute) * 60))
        return round_billsec(self.duration_seconds)


def _dec(value) -> Decimal | None:
    """Parse a BulkVS money/rate string. They are inconsistent — rates come back as both
    '.004' and '0.0003' — and Decimal(str) handles both. Returns None if unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001 - a malformed figure must flag the row, not crash the scan
        return None


def parse_voice_record(rec: dict, tz_name: str = VOICE_DEFAULT_TZ) -> VoiceCharge | None:
    """Normalize one /voice record. Pure. Returns None if it carries no usable identity.

    A record whose `amount` cannot be parsed is returned FLAGGED (unrated) rather than
    costed at zero — same honesty rule as everywhere else: a charge we can't read must be
    visible, never silently free.
    """
    from datetime import datetime, timezone as _tz
    from zoneinfo import ZoneInfo

    call_ref = str(rec.get("callID") or rec.get("CallID") or "").strip()
    if not call_ref:
        return None

    started_at = None
    raw_start = str(rec.get("callStart") or "").strip()
    if raw_start:
        try:
            naive = datetime.strptime(raw_start, "%Y-%m-%d %H:%M:%S")
            started_at = naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(_tz.utc)
        except Exception:  # noqa: BLE001 - a bad timestamp must not drop a real charge
            started_at = None

    try:
        duration = int(str(rec.get("durationSecs") or 0).strip() or 0)
    except (TypeError, ValueError):
        duration = 0

    amount = _dec(rec.get("amount"))
    return VoiceCharge(
        call_ref=call_ref,
        direction=str(rec.get("callType") or "").strip().lower() or "unknown",
        started_at=started_at,
        duration_seconds=max(duration, 0),
        source=str(rec.get("callSource") or "").strip() or None,
        destination=str(rec.get("callDestination") or "").strip() or None,
        per_minute=_dec(rec.get("perMinute")),
        amount=amount,
        trunk_group=str(rec.get("trunkGroup") or "").strip() or None,
        cnam=str(rec.get("Cnam") or "").strip() or None,
        unrated=amount is None,
        unrated_reason=None if amount is not None else "BulkVS reported no amount",
    )
