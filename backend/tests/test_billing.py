"""Unit test for the BulkVS cost kernel (app/services/billing.py).

No DB, no network — the kernel is stdlib-only by design, like app.flows.interpreter and
app.services.inbox_threads.

Every fixture below is a REAL record pulled from this account's BulkVS /voice feed, so the
tests pin behaviour against what the carrier actually billed rather than what we assumed.
Notably they encode the two facts that overturned the original estimate-based design:
  - a flow-forwarded call bills TWICE, seconds apart (inbound + outbound);
  - outbound is NOT a flat $0.004 — one observed call rated at $0.0099/min.

Run: python -m tests.test_billing
"""

import sys
from decimal import Decimal

from app.services import billing as b


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"billing failed at: {name}")


# --- real records from the live /voice feed -------------------------------------------------

INBOUND = {
    "callStart": "2026-07-28 15:04:41", "durationSecs": "16",
    "callSource": "+12178584185", "callDestination": "15618788090",
    "callID": "422088617_50304226@206.146.99.24",
    "perMinute": "0.0003", "amount": "0.00009", "callType": "inbound",
    "Cnam": "THOMASBORO   IL", "trunkGroup": "vps-main-trunk",
}
# The SAME call, 8 seconds later: the flow forwarding back out over the trunk.
FORWARD = {
    "callStart": "2026-07-28 15:04:49", "durationSecs": "7",
    "callSource": "+12178584185", "callDestination": "19549147244",
    "callID": "a2fddc3d-8366-4f2f-af28-fc6ba1a521d9",
    "perMinute": ".004", "amount": "0.00080", "callType": "outbound",
    "trunkGroup": "144.126.138.157",
}
# An outbound call rated well above "domestic" — proof a single flat outbound rate is wrong.
EXPENSIVE = {
    "callStart": "2026-07-24 10:47:01", "durationSecs": "11",
    "callSource": "+16452516222", "callDestination": "15616905516",
    "callID": "expensive-1", "perMinute": "0.0099", "amount": "0.00198",
    "callType": "outbound",
}
LONG_CALL = {
    "callStart": "2026-07-24 01:22:09", "durationSecs": "140",
    "callSource": "+16452516222", "callDestination": "19549147244",
    "callID": "long-1", "perMinute": ".004", "amount": "0.00960", "callType": "outbound",
}


def test_parse_basics():
    print("parse_voice_record — normalizing BulkVS's rated records:")
    c = b.parse_voice_record(INBOUND)
    check("record parses", c is not None)
    check("callID becomes the idempotency key", c.call_ref == INBOUND["callID"])
    check("callType -> direction", c.direction == "inbound")
    check("duration preserved", c.duration_seconds == 16)
    check("perMinute parsed", c.per_minute == Decimal("0.0003"))
    check("amount parsed (THEIR figure, not ours)", c.amount == Decimal("0.00009"))
    check("delivered CNAM captured but not billed", c.cnam == "THOMASBORO   IL")
    check("not flagged unrated", not c.unrated)

    # BulkVS writes rates inconsistently: '.004' on one record, '0.0003' on another.
    check("leading-dot rate parses", b.parse_voice_record(FORWARD).per_minute == Decimal(".004"))
    check("no callID -> dropped", b.parse_voice_record({"callStart": "x"}) is None)


def test_timezone():
    print("\ncallStart is account-local, NOT UTC:")
    # Verified against the same call in Asterisk's UTC CDR: 15:04:41 BulkVS == 19:04 UTC.
    c = b.parse_voice_record(INBOUND)
    check("15:04:41 Eastern -> 19:04 UTC",
          c.started_at.hour == 19 and c.started_at.minute == 4)
    check("stored as aware UTC", c.started_at.tzinfo is not None
          and c.started_at.utcoffset().total_seconds() == 0)
    # Treating it as UTC would file the charge 4h early and in the wrong day bucket.
    utc = b.parse_voice_record(INBOUND, tz_name="UTC")
    check("timezone is honoured, not hardcoded", utc.started_at.hour == 15)


def test_billed_seconds_from_their_numbers():
    print("\nbilled_seconds — derived from THEIR amount/rate so it always reconciles:")
    check("16s call billed as 18s (6s increment)",
          b.parse_voice_record(INBOUND).billed_seconds == 18)
    check("7s call billed as 12s", b.parse_voice_record(FORWARD).billed_seconds == 12)
    check("140s call billed as 144s", b.parse_voice_record(LONG_CALL).billed_seconds == 144)
    check("11s call billed as 12s", b.parse_voice_record(EXPENSIVE).billed_seconds == 12)


def test_forwarded_call_bills_twice():
    print("\nthe central fact: a forwarded call is TWO charges:")
    inb = b.parse_voice_record(INBOUND)
    out = b.parse_voice_record(FORWARD)
    check("distinct records", inb.call_ref != out.call_ref)
    check("8 seconds apart — same caller, one call",
          abs((out.started_at - inb.started_at).total_seconds()) == 8)
    check("opposite directions", inb.direction == "inbound" and out.direction == "outbound")
    total = inb.amount + out.amount
    check("the forward leg costs ~9x the inbound leg", out.amount > inb.amount * 8)
    check("call total is the SUM of both legs", total == Decimal("0.00089"))


def test_outbound_is_not_flat():
    print("\noutbound is not a flat rate (why a local rate table was wrong):")
    cheap = b.parse_voice_record(FORWARD)
    dear = b.parse_voice_record(EXPENSIVE)
    check("two outbound calls, different rates",
          cheap.per_minute == Decimal(".004") and dear.per_minute == Decimal("0.0099"))
    check("the dearer one is ~2.5x domestic", dear.per_minute > cheap.per_minute * Decimal("2"))


def test_unrated_is_flagged_not_zeroed():
    print("\na record with no readable amount is flagged, never counted as $0:")
    c = b.parse_voice_record({**INBOUND, "amount": None})
    check("flagged unrated", c.unrated)
    check("reason recorded", "no amount" in (c.unrated_reason or ""))
    check("amount stays None rather than 0", c.amount is None)
    junk = b.parse_voice_record({**INBOUND, "amount": "not-a-number"})
    check("unparseable amount also flagged", junk.unrated)


def test_rounding_helper():
    print("\nround_billsec — only a fallback now, still 6s increments:")
    check("unanswered bills nothing", b.round_billsec(0) == 0)
    for s, exp in [(1, 6), (6, 6), (7, 12), (16, 18), (140, 144)]:
        check(f"{s}s -> {exp}s", b.round_billsec(s) == exp)
    check("zero increment degrades to per-second, never ZeroDivisionError",
          b.round_billsec(15, increment_seconds=0, minimum_seconds=0) == 15)


def test_recurring_rate_table():
    print("\nSEED_RATES — now only load-bearing for RECURRING charges:")
    codes = [r["code"] for r in b.SEED_RATES]
    check("codes are unique", len(codes) == len(set(codes)))
    by_code = {r["code"]: r for r in b.SEED_RATES}
    for tier in ("0", "10", "1", "2", "3", "4", "AK", "PRI", "5", "6"):
        check(f"tier {tier} has a monthly rate", f"{b.DID_MONTHLY_PREFIX}{tier}" in by_code)
    check("tier 0 monthly = $0.06", by_code["did.monthly.tier.0"]["amount"] == "0.06")
    check("E911 = $0.49/number", by_code["e911.monthly"]["amount"] == "0.49")
    # $0.05 per BulkVS's own pricing page. A widely-cited third-party review says $0.25, but
    # that figure does not fit this account's observed balance: $25.00 funded and $24.76
    # remaining only reconciles with a $0.05 setup on the one PURCHASED number.
    check("DID setup = $0.05, not the $0.25 in circulation",
          by_code["did.setup"]["amount"] == "0.05")
    check("CNAM is priced (it IS billed, just outside the call record)",
          by_code["cnam.dip"]["amount"] == "0.0020")
    check("toll-free monthly = $0.14", by_code["did.monthly.tollfree"]["amount"] == "0.14")
    check("web-sourced rates stay identifiable",
          by_code["did.setup"]["source"] == "web"
          and by_code["did.monthly.tier.0"]["source"] == "sheet")
    # Live records confirm the published per-minute figures even though usage no longer uses
    # them for costing.
    check("published inbound rate matches what BulkVS actually charged",
          by_code["inbound.tier.0"]["amount"] == "0.0003"
          and b.parse_voice_record(INBOUND).per_minute == Decimal("0.0003"))

    print("\nis_toll_free — still used to pick the recurring rate:")
    check("800 is toll free", b.is_toll_free("+18005551212"))
    check("561 is not", not b.is_toll_free("+15618788090"))


def main():
    test_parse_basics()
    test_timezone()
    test_billed_seconds_from_their_numbers()
    test_forwarded_call_bills_twice()
    test_outbound_is_not_flat()
    test_unrated_is_flagged_not_zeroed()
    test_rounding_helper()
    test_recurring_rate_table()
    print("\nALL BILLING CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
