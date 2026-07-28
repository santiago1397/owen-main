"""Pure BulkVS cost-estimation kernel (Billing tab).

This is an ESTIMATE, never an invoice. BulkVS publishes no usage/billing API — every
path probed (/cdr, /usage, /invoice, /billing) returns the same 200-with-1-byte body as a
nonsense URL, i.e. they do not exist. So cost is derived from Asterisk's own CDR, which is
the only per-leg record of what actually traversed the trunk.

THE CENTRAL FACT: one caller-facing call can be TWO billable legs. An inbound call that a
flow forwards back out over the same trunk is billed by BulkVS as inbound minutes AND
outbound minutes. `calls` deliberately collapses every leg onto one row (provider_call_sid =
Linkedid), which is exactly the wrong shape for billing — hence a per-LEG projection.

Kept stdlib-only (no sqlalchemy, no app.core.config) so the classification, rounding and
rate-resolution rules are unit-testable in a bare sandbox, exactly like app.flows.interpreter
and app.services.inbox_threads. The DB-aware applier lives in app/workers/billing.py.

RATE PROVENANCE is tracked per rate row (`source`), because not every number is equally
trustworthy:
  - "sheet" — read off the operator's own BulkVS portal price table. Authoritative.
  - "web"   — filled from public BulkVS pricing where the portal sheet had a "View" link.
              Every item where the two overlap matches exactly (inbound $0.0003, outbound
              $0.004, toll-free $0.0055/$0.14, CNAM $0.002, E911 $0.49, DID $0.06 — 7 for 7),
              which is why the web-sourced gaps are trusted enough to seed.
Unknown/unresolvable rates are NEVER silently costed at $0 — the leg is marked `unrated`
and surfaced, because a billing estimate that quietly under-reports is worse than one that
admits ignorance.
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

# Charge kinds written to call_charges (one row per leg per kind).
KIND_MINUTES = "minutes"
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
    {"code": CNAM_DIP, "label": "CNAM lookup", "unit": "per_event",
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


# --- leg classification -------------------------------------------------------------------


@dataclass
class BillableLeg:
    """One leg of a call that traversed the BulkVS trunk, i.e. one thing BulkVS meters."""

    uniqueid: str
    linkedid: str
    direction: str            # "inbound" | "outbound"
    channel: str
    src: str | None
    dst: str | None
    raw_billsec: int
    # True when the CDR row records NO usable destination number at all — Asterisk writes
    # dst='s' (a dialplan extension) on the flow-dial leg, so a forwarded call's real
    # destination is not recoverable from CDR. This means "unreadable", NOT "foreign": a
    # destination we CAN read but that isn't NANP is a different case entirely (unrated),
    # and conflating the two would price international calls at the domestic rate.
    dest_unknown: bool = False


def is_toll_free(e164: str | None) -> bool:
    digits = "".join(c for c in (e164 or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits[:3] in TOLL_FREE_NPAS


def is_nanp(e164: str | None) -> bool:
    """True for a North-American-Numbering-Plan destination (the only outbound rate we hold).

    Deliberately strict: 10 digits, or 11 beginning with 1. Anything else (international,
    a dialplan extension like 's', an empty string) is NOT treated as domestic — it resolves
    to no rate code and the leg is flagged unrated rather than priced at the domestic rate.
    """
    digits = "".join(c for c in (e164 or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return True
    return len(digits) == 10


def classify_leg(row: dict, trunk_name: str = "bulkvs") -> BillableLeg | None:
    """Decide whether a CDR row is a BulkVS-billable leg, and in which direction.

    Billable iff the leg's own CHANNEL is on the BulkVS trunk (`PJSIP/<trunk>-…`) — that is
    literally what "traversed BulkVS" means. Operator WebRTC legs (`PJSIP/operator-…`) and
    internal `Local/…` legs never touch the carrier and are never billed.

    Direction reuses the entry-channel rule already locked in providers/asterisk.py: Asterisk
    sets a new channel's Linkedid to the Uniqueid of the first channel in the call, so the
    entry leg is the one whose `uniqueid == linkedid`.
      - entry leg on the trunk      -> the call ARRIVED from BulkVS  -> inbound
      - secondary leg on the trunk  -> we DIALED OUT over BulkVS     -> outbound
    That single rule handles both shapes correctly: an inbound call forwarded by a flow
    (entry=inbound + flow-dial=outbound), and a manual operator call (entry is the operator's
    non-trunk WebRTC leg and is not billed; the trunk leg is secondary -> outbound).

    Returns None for anything not billable.
    """
    channel = str(row.get("channel") or "")
    if not channel.startswith(f"PJSIP/{trunk_name}-"):
        return None

    uniqueid = str(row.get("uniqueid") or "")
    linkedid = str(row.get("linkedid") or "")
    if not uniqueid or not linkedid:
        return None

    direction = "inbound" if uniqueid == linkedid else "outbound"
    dst = row.get("dst")
    src = row.get("src")

    # billsec is answered seconds; unanswered legs are 0 and therefore cost nothing, but they
    # are still recorded so the log shows the call happened.
    try:
        raw_billsec = int(row.get("billsec") or 0)
    except (TypeError, ValueError):
        raw_billsec = 0

    # The destination only matters for OUTBOUND rating. "Unknown" means the row carries no
    # dialable number — a dialplan exten ('s'), an internal extension, or nothing — as
    # opposed to a readable number that happens to be international. Anything with fewer
    # digits than the shortest real destination is treated as not-a-number.
    dest_digits = "".join(c for c in str(dst or "") if c.isdigit())
    dest_unknown = direction == "outbound" and len(dest_digits) < 7

    return BillableLeg(
        uniqueid=uniqueid,
        linkedid=linkedid,
        direction=direction,
        channel=channel,
        src=str(src) if src else None,
        dst=str(dst) if dst else None,
        raw_billsec=max(raw_billsec, 0),
        dest_unknown=dest_unknown,
    )


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


# --- rate resolution ------------------------------------------------------------------------


@dataclass
class RateResolution:
    """Which rate code prices a leg, or why none does."""

    code: str | None
    unrated_reason: str | None = None

    @property
    def unrated(self) -> bool:
        return self.code is None


def resolve_rate_code(
    leg: BillableLeg,
    *,
    number_tier: str | None = None,
    number_phone: str | None = None,
    known_codes: set[str] | None = None,
) -> RateResolution:
    """Pick the rate code for a billable leg. Pure.

    INBOUND is priced off the DID that was dialed: toll-free numbers have their own rate
    regardless of tier, otherwise the BulkVS-reported tier selects the row. A DID whose tier
    we don't have (never synced, or a tier absent from the price sheet) is UNRATED — guessing
    a tier is how you under-report by 57x, since the sheet spans $0.0003 to $0.0198 per minute.

    OUTBOUND has exactly one published rate: domestic. A destination we can read and that is
    NOT NANP is unrated (international rates are published nowhere). A destination we CANNOT
    read — the flow-dial leg's dst='s' — is rated domestic, because this trunk only serves
    NANP and refusing to price the operator's most common call shape would gut the tab; the
    leg carries `dest_unknown` so the UI can show the uncertainty rather than hide it.
    """
    if leg.direction == "inbound":
        if is_toll_free(number_phone):
            code = INBOUND_TOLLFREE
        else:
            tier = (number_tier or "").strip()
            if not tier:
                return RateResolution(None, "no tier known for this DID")
            code = f"{INBOUND_TIER_PREFIX}{tier}"
        if known_codes is not None and code not in known_codes:
            return RateResolution(None, f"no rate configured for {code}")
        return RateResolution(code)

    # outbound
    if not leg.dest_unknown and not is_nanp(leg.dst):
        return RateResolution(None, f"no rate for non-domestic destination {leg.dst!r}")
    if known_codes is not None and OUTBOUND_DOMESTIC not in known_codes:
        return RateResolution(None, f"no rate configured for {OUTBOUND_DOMESTIC}")
    return RateResolution(OUTBOUND_DOMESTIC)
