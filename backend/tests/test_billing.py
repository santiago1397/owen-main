"""Unit test for the BulkVS cost kernel (app/services/billing.py).

No DB, no Asterisk, no network — the kernel is stdlib-only by design, like
app.flows.interpreter and app.services.inbox_threads, so the rating rules are proven in
isolation against REAL CDR rows captured from production (linkedid 1785265481.14).

Those fixtures include the two-leg shape that makes this feature necessary: an inbound call
the flow forwards back out over the same BulkVS trunk is billed TWICE — once inbound, once
outbound — which is exactly what a per-CALL cost view would hide.

Run: python -m tests.test_billing
"""

import sys
from decimal import Decimal

from app.services import billing as b

KNOWN = {r["code"] for r in b.SEED_RATES}


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"billing failed at: {name}")


# --- real production CDR rows -----------------------------------------------------------

INBOUND_LEG = {
    "uniqueid": "1785265481.14", "linkedid": "1785265481.14",
    "channel": "PJSIP/bulkvs-00000008", "dstchannel": "PJSIP/bulkvs-00000009",
    "src": "+12178584185", "dst": "+15618788090", "billsec": 15, "disposition": "ANSWERED",
}
FORWARD_LEG = {
    "uniqueid": "flow-dial-a01e3c45139a42a98afd83019ea60252", "linkedid": "1785265481.14",
    "channel": "PJSIP/bulkvs-00000009", "dstchannel": "",
    "src": "", "dst": "s", "billsec": 0, "disposition": "ANSWERED",
}
OPERATOR_LEG = {
    "uniqueid": "x.1", "linkedid": "x.1",
    "channel": "PJSIP/operator-admin-santiagoproperties.uk", "src": "", "dst": "", "billsec": 30,
}
LOCAL_LEG = {
    "uniqueid": "y.1", "linkedid": "y.1",
    "channel": "Local/cdrtest2@from-operator-00000002;1", "src": "", "dst": "", "billsec": 5,
}


def test_leg_classification():
    print("classify_leg — which legs BulkVS actually meters:")
    inbound = b.classify_leg(INBOUND_LEG)
    check("entry leg on the trunk -> inbound",
          inbound is not None and inbound.direction == "inbound")
    check("inbound keeps its answered seconds", inbound.raw_billsec == 15)

    fwd = b.classify_leg(FORWARD_LEG)
    check("secondary trunk leg -> outbound", fwd is not None and fwd.direction == "outbound")
    check("forward leg shares the call's linkedid (a 2nd charge on ONE call)",
          fwd.linkedid == INBOUND_LEG["linkedid"])

    check("operator WebRTC leg never billed", b.classify_leg(OPERATOR_LEG) is None)
    check("internal Local leg never billed", b.classify_leg(LOCAL_LEG) is None)
    check("trunk name is configurable",
          b.classify_leg(INBOUND_LEG, trunk_name="othertrunk") is None)
    check("missing linkedid is not billable",
          b.classify_leg({**INBOUND_LEG, "linkedid": ""}) is None)


def test_rounding():
    print("\nround_billsec — 6-second increments:")
    check("unanswered (billsec 0) bills nothing — the minimum is for CONNECTED calls",
          b.round_billsec(0) == 0)
    for billsec, expected in [(1, 6), (6, 6), (7, 12), (15, 18), (28, 30), (60, 60), (61, 66)]:
        check(f"{billsec}s -> {expected}s billed", b.round_billsec(billsec) == expected)
    check("increment is configurable (per-minute rounding)",
          b.round_billsec(15, increment_seconds=60, minimum_seconds=60) == 60)
    check("zero increment degrades to per-second, never ZeroDivisionError",
          b.round_billsec(15, increment_seconds=0, minimum_seconds=0) == 15)


def test_inbound_rating():
    print("\nresolve_rate_code — inbound prices off the DID's tier:")
    leg = b.classify_leg(INBOUND_LEG)
    r = b.resolve_rate_code(leg, number_tier="0", number_phone="+15618788090", known_codes=KNOWN)
    check("tier 0 DID -> inbound.tier.0", r.code == "inbound.tier.0" and not r.unrated)

    r = b.resolve_rate_code(leg, number_tier=None, number_phone="+15618788090", known_codes=KNOWN)
    check("no tier -> UNRATED, never guessed (tiers span $0.0003-$0.0198/min)",
          r.unrated and "tier" in (r.unrated_reason or ""))

    r = b.resolve_rate_code(leg, number_tier="0", number_phone="+18005551212", known_codes=KNOWN)
    check("toll-free overrides tier", r.code == "inbound.tollfree")

    r = b.resolve_rate_code(leg, number_tier="99", number_phone="+15618788090", known_codes=KNOWN)
    check("tier with no configured rate -> unrated", r.unrated)


def test_outbound_rating():
    print("\nresolve_rate_code — outbound, and the unreadable-vs-foreign distinction:")
    fwd = b.classify_leg(FORWARD_LEG)
    check("flow-dial leg's destination is unreadable (Asterisk writes dst='s')",
          fwd.dest_unknown is True)
    check("unreadable destination -> priced domestic (this trunk only serves NANP)",
          b.resolve_rate_code(fwd, known_codes=KNOWN).code == "outbound.domestic")

    intl = b.classify_leg({**FORWARD_LEG, "dst": "+442071234567", "billsec": 30})
    r = b.resolve_rate_code(intl, known_codes=KNOWN)
    check("readable NON-NANP destination is NOT unreadable", intl.dest_unknown is False)
    check("readable international -> UNRATED, must not fall through to the domestic rate",
          r.unrated and "non-domestic" in (r.unrated_reason or ""))

    dom = b.classify_leg({**FORWARD_LEG, "dst": "+19549147244", "billsec": 30})
    check("readable domestic -> outbound.domestic",
          dom.dest_unknown is False
          and b.resolve_rate_code(dom, known_codes=KNOWN).code == "outbound.domestic")


def test_costing():
    print("\ncost_of_minutes — exact at very small rates:")
    check("18 billed seconds @ $0.0003/min = $0.000090",
          b.cost_of_minutes(18, "0.0003") == Decimal("0.000090"))
    check("zero billed seconds costs nothing",
          b.cost_of_minutes(0, "0.0040") == Decimal("0.000000"))

    inbound = b.cost_of_minutes(b.round_billsec(180), "0.0003")
    outbound = b.cost_of_minutes(b.round_billsec(180), "0.0040")
    check("forwarding costs 13x receiving — the forward leg dominates a forwarded call",
          outbound > inbound and (outbound / inbound).quantize(Decimal("1")) == Decimal("13"))


def test_seed_table():
    print("\nSEED_RATES — the price sheet as data:")
    codes = [r["code"] for r in b.SEED_RATES]
    check("codes are unique", len(codes) == len(set(codes)))

    missing = [
        code
        for tier in ("0", "10", "1", "2", "3", "4", "AK", "PRI", "5", "6")
        for code in (f"{b.INBOUND_TIER_PREFIX}{tier}", f"{b.DID_MONTHLY_PREFIX}{tier}")
        if code not in KNOWN
    ]
    check("every tier the resolver can name is seeded (else it'd be unrated in prod)",
          not missing)
    check("non-tier codes seeded",
          all(c in KNOWN for c in
              (b.INBOUND_TOLLFREE, b.OUTBOUND_DOMESTIC, b.CNAM_DIP, b.E911_MONTHLY)))

    by_code = {r["code"]: r for r in b.SEED_RATES}
    check("inbound tier 0 = $0.0003/min", by_code["inbound.tier.0"]["amount"] == "0.0003")
    check("inbound tier 3 = $0.0171/min", by_code["inbound.tier.3"]["amount"] == "0.0171")
    check("outbound domestic = $0.0040/min", by_code["outbound.domestic"]["amount"] == "0.0040")
    check("tier 0 monthly = $0.06", by_code["did.monthly.tier.0"]["amount"] == "0.06")
    check("CNAM dip = $0.0020", by_code["cnam.dip"]["amount"] == "0.0020")
    check("E911 = $0.49/number", by_code["e911.monthly"]["amount"] == "0.49")

    check("web-sourced rates stay identifiable (provenance is load-bearing)",
          by_code["sms.outbound"]["source"] == "web"
          and by_code["did.setup"]["source"] == "web"
          and by_code["inbound.tier.0"]["source"] == "sheet")


def main():
    test_leg_classification()
    test_rounding()
    test_inbound_rating()
    test_outbound_rating()
    test_costing()
    test_seed_table()
    print("\nALL BILLING CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
