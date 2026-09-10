"""The hybrid ring group: softphones AND PSTN numbers, first to answer wins.

This is the one genuinely new piece of call logic in the CRM link
(`app/integrations/crm/ring.py`), so it is tested against a fake ARI that models the real
constraints rather than one that says yes to everything — the lesson
`tests/test_dial_record_bridge.py` records about a permissive fake hiding a live failure.

The fake here enforces:
  1. a channel with an active CHANNEL recording cannot join a bridge (ARI 409), and
     addChannel is all-or-nothing;
  2. a leg that has been DELETEd is gone — answering it afterwards is a test bug;
  3. each originate carries its own `callerId`, which is recorded per leg, because
     per-leg caller-ID is the entire reason this is not `AsteriskAriClient.ring_and_bridge`.

Run: python -m tests.test_crm_hybrid_ring
"""

import asyncio

CHAN = "1799000222.7"
DIALED = "+15615550200"
CALLER = "+15615559999"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_hybrid_ring failed at: {name}")


class FakeAri:
    """Models the ARI operations `hybrid_ring_and_bridge` actually uses."""

    def __init__(self, *, answers=None, refuse_originate=(), bridge_ok=True,
                 recording_channels=()):
        self.originated = []          # (channel_id, endpoint, caller_id)
        self.order = []               # every op in sequence, so ORDER can be asserted
        self.deleted = []             # channel ids DELETEd
        self.bridged = []
        self.bridge_recordings = []
        self.destroyed_bridges = []
        self._answers = answers       # channel index that answers, or None for nobody
        self._refuse = set(refuse_originate)
        self._bridge_ok = bridge_ok
        self._recording_channels = set(recording_channels)
        self._live = set()

    async def _post_json(self, path, params=None, json=None):
        params = params or {}
        endpoint = params.get("endpoint")
        cid = params.get("channelId")
        if endpoint in self._refuse:
            return None
        self.originated.append((cid, endpoint, params.get("callerId")))
        self.order.append(("originate", cid))
        self._live.add(cid)
        return {"id": cid}

    async def _delete(self, path):
        cid = path.rsplit("/", 1)[-1]
        self.deleted.append(cid)
        self.order.append(("delete", cid))
        self._live.discard(cid)
        return True

    async def _await_first_answer(self, queue, channel_id, out_ids, timeout_s):
        """Return the id of whichever originated leg the scenario says answers."""
        if self._answers is None:
            return None
        ordered = [cid for cid, _e, _c in self.originated]
        if self._answers >= len(ordered):
            return None
        winner = ordered[self._answers]
        assert winner in self._live, "the scenario answered a leg that was torn down"
        return winner

    async def _await_bridge_end(self, queue, channel_id, out_id):
        return {"dial_ended_by": "dialed", "dial_talk_ms": 42000}

    async def create_bridge(self):
        self.order.append(("create_bridge", None))
        return "bridge-1" if self._bridge_ok else None

    async def add_to_bridge(self, bridge_id, *channel_ids):
        # All-or-nothing, and ARI refuses a channel that is currently recording.
        if any(c in self._recording_channels for c in channel_ids):
            return False
        self.bridged.extend(channel_ids)
        return True

    async def record_bridge(self, bridge_id, name):
        self.bridge_recordings.append(name)

    async def destroy_bridge(self, bridge_id):
        self.destroyed_bridges.append(bridge_id)


def _legs(operators=("desk@x.com",), pstn=("+15615550111", "+15615550122")):
    from app.integrations.crm import ring

    return ring.build_legs(
        dialed=DIALED, caller_number=CALLER, bound_did=DIALED,
        operators=list(operators), pstn_numbers=list(pstn),
    )


def _run(ari, legs, **kw):
    from app.integrations.crm import ring

    return asyncio.run(ring.hybrid_ring_and_bridge(
        ari, CHAN, legs, timeout_s=kw.pop("timeout_s", 5), **kw))


def test_all_legs_are_originated_together():
    print("every configured destination is originated, softphone and PSTN alike:")
    ari = FakeAri(answers=None)
    legs = _legs()
    result = _run(ari, legs)

    endpoints = [e for _c, e, _cid in ari.originated]
    check("three legs originated", len(ari.originated) == 3)
    check("the softphone leg is a PJSIP operator endpoint",
          "PJSIP/operator-desk-x.com" in endpoints)
    check("both PSTN legs go out over the trunk",
          sum(1 for e in endpoints if "@" in e) == 2)
    check("every leg carries the flow-dial channel prefix",
          all(c.startswith("flow-dial-") for c, _e, _ci in ari.originated))
    check("nobody answered -> noanswer", result.port == "noanswer")


def test_per_leg_caller_id():
    """The reason this is not ring_and_bridge. A softphone leg shows the dialed DID as the
    display name so the browser popup can say which number was called; a PSTN leg must
    present the BOUND DID as the number, because sending the caller's own number out over
    the trunk is caller-ID spoofing."""
    print("each leg carries its OWN caller-ID:")
    ari = FakeAri(answers=None)
    _run(ari, _legs())

    by_endpoint = {e: cid for _c, e, cid in ari.originated}
    op_cid = by_endpoint["PJSIP/operator-desk-x.com"]
    pstn_cid = by_endpoint["PJSIP/+15615550111@bulkvs"]

    check("operator leg: display name is the DIALED DID", op_cid.startswith(DIALED + " <"))
    check("operator leg: URI user is the CALLER", op_cid.endswith(f"<{CALLER}>"))
    check("PSTN leg: the NUMBER presented is the bound DID", pstn_cid.endswith(f"<{DIALED}>"))
    check("PSTN leg does NOT present the caller's number as the number",
          not pstn_cid.endswith(f"<{CALLER}>"))
    check("the two caller-IDs genuinely differ", op_cid != pstn_cid)


def test_first_to_answer_is_bridged_and_the_rest_are_torn_down():
    print("first to answer is bridged; every other leg is torn down:")
    ari = FakeAri(answers=1)                    # the FIRST PSTN leg picks up
    legs = _legs()
    result = _run(ari, legs, record_name="lid-crmlink-1")

    winner_id = ari.originated[1][0]
    losers = [c for c, _e, _ci in ari.originated if c != winner_id]

    check("reported answered", result.port == "answered")
    check("the winner is the PSTN destination", result.winning_kind == "pstn")
    check("the winning destination is named", result.winning_destination == "+15615550111")
    check("caller and winner were bridged", set(ari.bridged) == {CHAN, winner_id})
    check("EVERY other leg was hung up", all(c in ari.deleted for c in losers))
    # Ordering matters: a second phone must not be answerable into a call that is already
    # being connected, and nobody's cell should keep ringing after the desk picked up.
    bridge_at = [i for i, (op, _v) in enumerate(ari.order) if op == "create_bridge"][0]
    loser_deletes = [i for i, (op, v) in enumerate(ari.order)
                     if op == "delete" and v in losers]
    check("both losers were hung up", len(losers) == 2 and len(loser_deletes) == 2)
    check("and hung up BEFORE the bridge was created",
          all(i < bridge_at for i in loser_deletes))
    check("the BRIDGE was recorded, never a channel",
          ari.bridge_recordings == ["lid-crmlink-1"])
    check("the bridge was destroyed on the way out", ari.destroyed_bridges == ["bridge-1"])
    check("the winner is dropped too, after the bridge ended", winner_id in ari.deleted)


def test_a_softphone_can_win_too():
    print("the softphone leg wins when it answers first:")
    ari = FakeAri(answers=0)
    result = _run(ari, _legs())
    check("answered", result.port == "answered")
    check("winner is the operator", result.winning_kind == "operator")
    check("named by operator id", result.winning_destination == "desk@x.com")


def test_a_dead_destination_does_not_cancel_the_group():
    """The whole point of ringing three phones is that any one of them can be unreachable."""
    print("one destination that will not originate does not kill the ring:")
    ari = FakeAri(answers=0, refuse_originate={"PJSIP/+15615550111@bulkvs"})
    result = _run(ari, _legs())

    check("only the reachable legs were originated", len(ari.originated) == 2)
    check("the dead one is reported", result.failed_to_originate == ["+15615550111"])
    check("the group still connected", result.port == "answered")


def test_no_leg_at_all_is_a_failure_not_a_silent_answer():
    print("if nothing can be originated, the group reports failure:")
    ari = FakeAri(answers=0, refuse_originate={
        "PJSIP/operator-desk-x.com",
        "PJSIP/+15615550111@bulkvs",
        "PJSIP/+15615550122@bulkvs",
    })
    result = _run(ari, _legs())
    check("failed", result.port == "failed")
    check("and says why", result.reason == "no leg could be originated")
    check("nothing was bridged", ari.bridged == [])


def test_a_rejected_bridge_is_reported_as_failed_not_answered():
    """Regression, inherited from the 2026-08-05 live failure: reporting `answered` on a
    bridge ARI refused left both parties on 25 seconds of dead air."""
    print("a rejected bridge surfaces as failed, so the handler can go to voicemail:")
    ari = FakeAri(answers=0, recording_channels={CHAN})
    result = _run(ari, _legs())

    check("failed, not answered", result.port == "failed")
    check("and says why", result.reason == "bridge_rejected")
    check("no legs were joined", ari.bridged == [])
    check("every leg was still torn down",
          all(c in ari.deleted for c, _e, _ci in ari.originated))


def test_empty_group_is_a_clean_noanswer():
    print("a binding with no destinations is a clean noanswer, not a crash:")
    ari = FakeAri(answers=0)
    result = _run(ari, [])
    check("noanswer", result.port == "noanswer")
    check("says why", result.reason == "no ring destinations")
    check("nothing was originated", ari.originated == [])


if __name__ == "__main__":
    test_all_legs_are_originated_together()
    test_per_leg_caller_id()
    test_first_to_answer_is_bridged_and_the_rest_are_torn_down()
    test_a_softphone_can_win_too()
    test_a_dead_destination_does_not_cancel_the_group()
    test_no_leg_at_all_is_a_failure_not_a_silent_answer()
    test_a_rejected_bridge_is_reported_as_failed_not_answered()
    test_empty_group_is_a_clean_noanswer()
    print("\nALL CRM HYBRID-RING CHECKS PASSED")
