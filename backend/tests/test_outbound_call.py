"""Unit tests for manual operator OUTBOUND calling (Ticket 14).

Dependency-free by design (like test_webrtc_credentials): exercises the PURE
app.telephony.control.place_outbound_call orchestration with a FAKE ARI client, plus the pure
guardrails / from-number restriction / defensive opt-out resolution in app.telephony.outbound.
It does NOT import fastapi/sqlalchemy (absent in the sandbox), so it does not exercise the thin
FastAPI wrapper in app/api/telephony.py directly.

Asserts:
- place_outbound_call drives EXACTLY the right ARI ops in order: originate operator leg ->
  originate callee (owned-DID caller-ID, linked via originator) -> PRE-BRIDGE consent play to
  the CALLEE -> create+add bridge -> record. Consent play happens BEFORE the bridge.
- the operator_channel_id path skips the operator originate; failures short-circuit.
- from-number restriction: only active, non-released, owner=bulkvs DIDs are allowed.
- time-window guardrail: warns outside 8am–9pm in the callee's (area-code) local time.
- opt-out: resolve_opt_out_model() is None when the Ticket-10 table/model is absent (skipped
  silently); opt_out_warning maps True/False/None correctly.

NOTE (unrun here): the AUTHZ + ASTERISK_ENABLED gate on POST /outbound/call, the DB owned-DID
query, and the end-to-end ARI/DB projection that yields the ONE outbound `calls` row
(direction='outbound' from the X_OWEN_DIRECTION channel var, campaign via the from-number) live
in app/api/telephony.py + services/ingestion.py + providers/asterisk.py and need fastapi + a live
Asterisk + Postgres — not importable/runnable in this sandbox. Those paths are asserted
structurally only; run them under the full backend venv against a live stack.

Run: python -m tests.test_outbound_call
"""

import asyncio
import sys
from datetime import datetime, timezone

from app.telephony import control, outbound


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"outbound_call failed at: {name}")


def _run(coro):
    return asyncio.run(coro)


class FakeOutboundAri:
    """Records every outbound ARI op in order; scriptable channel/bridge ids + failure points."""

    def __init__(self, *, op_channel="op-chan", callee_channel="callee-chan", bridge_id="bridge-1"):
        self.ops = []
        self._op_channel = op_channel
        self._callee_channel = callee_channel
        self._bridge_id = bridge_id

    async def originate_operator(self, operator_id, *, caller_id=None, variables=None):
        self.ops.append(("originate_operator", operator_id, caller_id, variables))
        return self._op_channel

    async def originate_number(self, number, *, caller_id=None, trunk_name=None,
                              originator=None, variables=None):
        self.ops.append(("originate_number", number, caller_id, trunk_name, originator, variables))
        return self._callee_channel

    async def play(self, channel_id, media):
        self.ops.append(("play", channel_id, media))

    async def create_bridge(self):
        self.ops.append(("create_bridge", None))
        return self._bridge_id

    async def add_to_bridge(self, bridge_id, *channel_ids):
        self.ops.append(("add_to_bridge", bridge_id, channel_ids))

    async def record_bridge(self, bridge_id, name):
        self.ops.append(("record_bridge", bridge_id, name))


def test_place_outbound_call_sequence():
    print("place_outbound_call drives originate -> consent -> bridge -> record, in order:")
    ari = FakeOutboundAri()
    result = _run(control.place_outbound_call(
        ari,
        operator_id="op@x.com",
        callee_number="+13055551234",
        from_number="+17865550000",
        trunk_name="bulkvs",
        consent_media="sound:owen/outbound-recording-consent",
        record=True,
    ))
    kinds = [o[0] for o in ari.ops]
    check("op order = originate_operator, originate_number, play, create_bridge, add_to_bridge, record_bridge",
          kinds == ["originate_operator", "originate_number", "play", "create_bridge",
                    "add_to_bridge", "record_bridge"])

    # Consent play happens BEFORE the bridge is created (the outbound consent invariant).
    check("consent play precedes bridge creation", kinds.index("play") < kinds.index("create_bridge"))
    play_op = next(o for o in ari.ops if o[0] == "play")
    check("consent is played to the CALLEE channel", play_op[1] == "callee-chan")

    # The owned DID is the caller-ID on the callee leg; the legs are linked via originator.
    orig_num = next(o for o in ari.ops if o[0] == "originate_number")
    check("callee originated with owned-DID caller-ID", orig_num[2] == "+17865550000")
    check("callee linked onto the operator leg (originator)", orig_num[4] == "op-chan")
    check("outbound direction stamped on the legs",
          orig_num[5] == {control.DIRECTION_VAR: "outbound", control.FROM_VAR: "+17865550000"})

    # Bridge adds BOTH legs; recording name is Linkedid-prefixed (attaches to the calls row).
    add_op = next(o for o in ari.ops if o[0] == "add_to_bridge")
    check("bridge adds operator + callee", add_op[2] == ("op-chan", "callee-chan"))
    rec_op = next(o for o in ari.ops if o[0] == "record_bridge")
    check("recording name is prefixed with the operator (entry) channel id",
          rec_op[2] == "op-chan-outbound")
    check("result ok with bridge + channel ids",
          result["ok"] and result["bridge_id"] == "bridge-1"
          and result["operator_channel"] == "op-chan" and result["callee_channel"] == "callee-chan")


def test_place_outbound_call_variants():
    print("place_outbound_call — existing operator leg, no-record, and failure short-circuits:")
    # operator_channel_id supplied => skip originate_operator.
    ari = FakeOutboundAri()
    _run(control.place_outbound_call(
        ari, operator_id="op@x.com", callee_number="+13055551234", from_number="+17865550000",
        trunk_name="bulkvs", consent_media=None, record=False, operator_channel_id="live-op-1",
    ))
    kinds = [o[0] for o in ari.ops]
    check("no originate_operator when a live channel is supplied", "originate_operator" not in kinds)
    check("no consent play when consent_media is None", "play" not in kinds)
    check("no record when record=False", "record_bridge" not in kinds)
    add_op = next(o for o in ari.ops if o[0] == "add_to_bridge")
    check("supplied operator channel is bridged", add_op[2] == ("live-op-1", "callee-chan"))

    # operator originate fails -> short-circuit, nothing else attempted.
    op_fail = FakeOutboundAri(op_channel=None)
    r1 = _run(control.place_outbound_call(
        op_fail, operator_id="o", callee_number="+13055551234", from_number="+17865550000",
        trunk_name="bulkvs"))
    check("operator originate failure -> ok False", not r1["ok"] and r1["reason"] == "operator_originate_failed")
    check("nothing after a failed operator originate", [o[0] for o in op_fail.ops] == ["originate_operator"])

    # callee originate fails -> short-circuit before consent/bridge.
    callee_fail = FakeOutboundAri(callee_channel=None)
    r2 = _run(control.place_outbound_call(
        callee_fail, operator_id="o", callee_number="+13055551234", from_number="+17865550000",
        trunk_name="bulkvs", consent_media="sound:x"))
    check("callee originate failure -> ok False", not r2["ok"] and r2["reason"] == "callee_originate_failed")
    check("no consent/bridge after a failed callee originate",
          [o[0] for o in callee_fail.ops] == ["originate_operator", "originate_number"])

    # bridge creation fails -> ok False, no addChannel/record.
    bridge_fail = FakeOutboundAri(bridge_id=None)
    r3 = _run(control.place_outbound_call(
        bridge_fail, operator_id="o", callee_number="+13055551234", from_number="+17865550000",
        trunk_name="bulkvs", consent_media="sound:x"))
    check("bridge failure -> ok False", not r3["ok"] and r3["reason"] == "bridge_failed")
    check("no addChannel/record on a failed bridge",
          [o[0] for o in bridge_fail.ops] == ["originate_operator", "originate_number", "play", "create_bridge"])


class FakeNumber:
    def __init__(self, phone_number, owner_provider="bulkvs", active=True, released_at=None,
                 provider_status=None):
        self.phone_number = phone_number
        self.owner_provider = owner_provider
        self.active = active
        self.released_at = released_at
        self.provider_status = provider_status


def test_from_number_restriction():
    print("from-number restriction — only active, non-released, carrier-Active, owner=bulkvs DIDs:")
    rows = [
        FakeNumber("+17865550000"),                                   # owned, allowed
        FakeNumber("+13055551111", owner_provider="twilio"),          # foreign owner, denied
        FakeNumber("+13055552222", active=False),                     # inactive, denied
        FakeNumber("+13055553333", released_at=datetime(2026, 1, 1)), # soft-released, denied
        FakeNumber("+13055554444", provider_status="SUBMITTED"),      # pending port-in, denied
        FakeNumber("+17865550001", provider_status="Active"),         # carrier-active, allowed
    ]
    allowed = outbound.owned_from_number_set(rows, owner_provider="bulkvs")
    check("only owned/active/non-released/carrier-Active BulkVS DIDs are allowed",
          allowed == {"+17865550000", "+17865550001"})
    check("foreign owner rejected",
          not outbound.is_owned_bulkvs_did(rows[1], owner_provider="bulkvs"))
    check("inactive rejected", not outbound.is_owned_bulkvs_did(rows[2], owner_provider="bulkvs"))
    check("released rejected", not outbound.is_owned_bulkvs_did(rows[3], owner_provider="bulkvs"))
    check("carrier SUBMITTED rejected",
          not outbound.is_owned_bulkvs_did(rows[4], owner_provider="bulkvs"))


def test_time_window_guardrail():
    print("time-window guardrail — soft warning outside 8am–9pm in the callee's local time:")
    # 02:00 UTC: Eastern (-5) -> 21:00 (>=21, warn); Pacific (-8) -> 18:00 (inside, no warn).
    now = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    eastern = outbound.time_window_warning("+13055550000", now)   # 305 = Eastern
    pacific = outbound.time_window_warning("+12135550000", now)   # 213 = Pacific
    check("Eastern callee at 21:00 local -> warning", eastern is not None)
    check("Pacific callee at 18:00 local -> no warning", pacific is None)

    check("area code parsed from 11-digit +1", outbound.area_code("+13055550000") == "305")
    check("area code parsed from bare 10-digit", outbound.area_code("3055550000") == "305")
    check("unknown area code -> Eastern default offset",
          outbound.utc_offset_for_number("+19995550000") == outbound.DEFAULT_UTC_OFFSET)

    # Boundary hours (via a Pacific number so local hour == UTC hour - 8).
    def pac_hour_warn(utc_hour):
        n = datetime(2026, 7, 22, utc_hour, 0, tzinfo=timezone.utc)
        return outbound.time_window_warning("+12135550000", n)
    check("07:59-ish -> hour 7 local -> warn", pac_hour_warn(15) is not None)   # 15-8 = 7
    check("hour 8 local -> inside (no warn)", pac_hour_warn(16) is None)         # 16-8 = 8
    check("hour 20 local -> inside (no warn)", pac_hour_warn(4) is None)         # 4+24-8 = 20
    check("hour 21 local -> warn", pac_hour_warn(5) is not None)                 # 5+24-8 = 21


def test_opt_out_defensive():
    print("opt-out guardrail — resolves now that Ticket 10's sms_opt_outs model has merged:")
    # Ticket 10 (manual outbound SMS + opt-out gate) has since merged, so the model exists
    # and the resolver must find it. `resolve_opt_out_model()` staying import-light/defensive
    # (returning None on any import error) is still exercised by test_number_sync-style callers
    # that don't have the model layer available at all.
    check("resolve_opt_out_model() resolves the merged SmsOptOut model",
          outbound.resolve_opt_out_model() is not None)
    check("opt_out_warning(None) -> no warning (undetermined => skip)",
          outbound.opt_out_warning(None) is None)
    check("opt_out_warning(False) -> no warning", outbound.opt_out_warning(False) is None)
    check("opt_out_warning(True) -> warning", outbound.opt_out_warning(True) is not None)


def main():
    test_place_outbound_call_sequence()
    test_place_outbound_call_variants()
    test_from_number_restriction()
    test_time_window_guardrail()
    test_opt_out_defensive()
    print("\nALL OUTBOUND CALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
