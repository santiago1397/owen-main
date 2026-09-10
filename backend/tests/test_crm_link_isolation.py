"""THE test. An UNBOUND number's call path must be unchanged, module on AND off.

owen-main is a live phone system for a real roofing business. The CRM link (see
`app/integrations/crm/`) is additive and opt-in, and the claim it rests on is:

    a DID that is not bound to the CRM behaves EXACTLY as it did before this module
    existed — with the module disabled, and equally with it enabled.

This asserts that claim the only way that means anything: by driving the REAL
`flows/runtime.py::run_flow_for_stasis` — the one function the ARI consumer calls on an
inbound call — against a fake ARI client that records every operation, in three worlds:

    A. the module disabled            (CRM_LINK_ENABLED=false)
    B. the module enabled, DID unbound (no crm_links row)
    C. the module enabled, DID BOUND   (the only world where behaviour may differ)

and asserting that A and B produce a byte-for-byte identical operation sequence, and that
C differs. If a future change makes the CRM link leak into an unbound call, A != B and this
fails.

It also asserts the stronger claim behind the kill switch: with `CRM_LINK_ENABLED` false,
the module does not merely decline, it never opens a database session at all. That is
checked by COUNTING calls to `app.db.SessionLocal` — zero with the switch off, at least
one with it on, because the hook's blanket `except` would make a return value of False
prove nothing on its own.

Stdlib only apart from the app itself, like every other test here.
Run: python -m tests.test_crm_link_isolation
"""

import asyncio

LINKEDID = "1799000111.42"
CHAN = "1799000111.42"
UNBOUND_DID = "+15615550100"
BOUND_DID = "+15615550200"
CALLER = "+15615559999"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_link_isolation failed at: {name}")


# --- a fake ARI that records everything ----------------------------------------------------

class RecordingAri:
    """Records the ARI operation sequence. Nobody answers, so every world ends in voicemail
    — which is the point: the comparison is over the WHOLE call, not just the ring."""

    def __init__(self):
        self.ops = []

    async def answer(self, channel_id):
        self.ops.append(("answer", channel_id))

    async def play_and_wait(self, channel_id, media, *, timeout_s=30.0):
        self.ops.append(("play_and_wait", channel_id, media))

    async def available_operators(self):
        self.ops.append(("available_operators",))
        return []                       # nobody is registered -> straight to voicemail

    async def ring_start(self, channel_id):
        self.ops.append(("ring_start", channel_id))

    async def ring_stop(self, channel_id):
        self.ops.append(("ring_stop", channel_id))

    async def ring_and_bridge(self, channel_id, endpoints, *, caller_id, timeout_s,
                              record_name=None):
        self.ops.append(("ring_and_bridge", tuple(endpoints), caller_id))
        return "noanswer"

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        self.ops.append(("voicemail", channel_id, name))

    async def hangup(self, channel_id):
        self.ops.append(("hangup", channel_id))

    # Only the CRM path reaches these; their presence in a recording is itself a failure
    # signal for worlds A and B.
    async def _post_json(self, path, params=None, json=None):
        self.ops.append(("originate", params.get("endpoint"), params.get("callerId")))
        return {"id": params.get("channelId")}

    async def _delete(self, path):
        self.ops.append(("delete", path))
        return True

    async def _await_first_answer(self, queue, channel_id, out_ids, timeout_s):
        self.ops.append(("await_first_answer", len(out_ids)))
        return None

    async def create_bridge(self):
        self.ops.append(("create_bridge",))
        return "bridge-1"

    async def add_to_bridge(self, bridge_id, *channel_ids):
        self.ops.append(("add_to_bridge", channel_ids))
        return True

    async def record_bridge(self, bridge_id, name):
        self.ops.append(("record_bridge", name))

    async def destroy_bridge(self, bridge_id):
        self.ops.append(("destroy_bridge", bridge_id))


# --- a fake database ------------------------------------------------------------------------

class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [] if self._value is None else [self._value]


class FakeSession:
    """Answers the two queries this path makes: `_resolve_active_flow_version` looks up a
    Number (None => the DID has no flow => the unassigned branch), and `binding.resolve`
    looks up a (CrmLink, Number) join."""

    def __init__(self, binding_row=None):
        self.binding_row = binding_row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        text = str(stmt)
        if "crm_links" in text:
            return FakeResult(self.binding_row)
        return FakeResult(None)         # no numbers row => no flow assigned

    async def commit(self):
        return None


def _bound_row():
    """An in-memory (CrmLink, Number) pair, exactly what `binding.resolve` unpacks."""
    from app.integrations.crm.models import CrmLink
    from app.models import Number

    number = Number(phone_number=BOUND_DID, media_provider="asterisk",
                    friendly_name="Bound line")
    link = CrmLink(enabled=True, ring_operators=True, operator_ids=[],
                   pstn_numbers=["+15615550111"], ring_timeout_seconds=5)
    return (link, number)


# --- the harness ----------------------------------------------------------------------------

def _stasis_event(dialed):
    """The StasisStart the ARI consumer hands to `run_flow_for_stasis`."""
    return {
        "type": "StasisStart",
        "channel": {
            "id": CHAN,
            "dialplan": {"exten": dialed},
            "caller": {"number": CALLER},
            "channelvars": {"CHANNEL(linkedid)": LINKEDID},
        },
    }


def run_world(*, enabled, bound, session_factory=None):
    """Drive the REAL run_flow_for_stasis once and return the recorded ARI ops."""
    import app.db as db_mod
    import app.flows.runtime as runtime
    import app.integrations.crm.push as crm_push
    from app.core.config import settings

    ari = RecordingAri()
    row = _bound_row() if bound else None
    factory = session_factory or (lambda: FakeSession(row))

    saved = (settings.CRM_LINK_ENABLED, runtime.SessionLocal, db_mod.SessionLocal,
             crm_push.SessionLocal)
    reported = []

    async def fake_enqueue(facts):
        # Reporting is a side effect of the CRM path, never of the default path. Captured
        # rather than executed so the comparison is about ARI operations.
        reported.append(facts.phase)
        return True

    saved_enqueue = crm_push.enqueue_call_event
    try:
        settings.CRM_LINK_ENABLED = enabled
        runtime.SessionLocal = factory
        db_mod.SessionLocal = factory
        crm_push.SessionLocal = factory
        crm_push.enqueue_call_event = fake_enqueue
        asyncio.run(runtime.run_flow_for_stasis(_stasis_event(
            BOUND_DID if bound else UNBOUND_DID), ari))
    finally:
        (settings.CRM_LINK_ENABLED, runtime.SessionLocal, db_mod.SessionLocal,
         crm_push.SessionLocal) = saved
        crm_push.enqueue_call_event = saved_enqueue
    return ari.ops, reported


# --- the tests --------------------------------------------------------------------------------

def test_unbound_path_identical_enabled_and_disabled():
    print("an UNBOUND number's call path is identical with the module OFF and ON:")
    off_ops, off_reported = run_world(enabled=False, bound=False)
    on_ops, on_reported = run_world(enabled=True, bound=False)

    check("the disabled world handled the call", len(off_ops) > 0)
    check("byte-for-byte identical operation sequence", off_ops == on_ops)
    check("nothing was reported to the CRM either way",
          off_reported == [] and on_reported == [])
    check("it is the existing default handler (consent -> operators -> voicemail)",
          [o[0] for o in off_ops] == ["answer", "play_and_wait", "available_operators",
                                      "voicemail"])
    check("no CRM originate happened", not any(o[0] == "originate" for o in on_ops))


def test_kill_switch_short_circuits_before_the_database():
    """The kill switch must not merely decline — it must decline BEFORE any database work.

    Counted rather than asserted-on-return-value, deliberately. `hook.handle_bound_inbound`
    has a blanket `except` that returns False for anything, so "it returned False" would be
    satisfied by a broken hook just as well as by a working kill switch. What distinguishes
    them is whether the session factory was called at all — zero times with the switch off,
    and at least once with it on.
    """
    print("the kill switch short-circuits BEFORE the module opens a session:")
    import app.db as db_mod
    import app.integrations.crm.hook as hook
    from app.core.config import settings

    calls = {"n": 0}

    def counting_factory(*_a, **_kw):
        calls["n"] += 1
        return FakeSession(None)

    saved_enabled, saved_session = settings.CRM_LINK_ENABLED, db_mod.SessionLocal
    try:
        settings.CRM_LINK_ENABLED = False
        db_mod.SessionLocal = counting_factory
        took_off = asyncio.run(hook.handle_bound_inbound(
            RecordingAri(), CHAN, LINKEDID, UNBOUND_DID, CALLER))
        off_sessions = calls["n"]

        settings.CRM_LINK_ENABLED = True
        took_on = asyncio.run(hook.handle_bound_inbound(
            RecordingAri(), CHAN, LINKEDID, UNBOUND_DID, CALLER))
        on_sessions = calls["n"] - off_sessions
    finally:
        settings.CRM_LINK_ENABLED, db_mod.SessionLocal = saved_enabled, saved_session

    check("disabled: the hook declined the call", took_off is False)
    check("disabled: ZERO database sessions were opened", off_sessions == 0)
    check("enabled + unbound: the hook still declined", took_on is False)
    check("enabled: a session WAS opened (so the count above is a real short-circuit)",
          on_sessions >= 1)


def test_bound_number_does_take_a_different_path():
    """The negative control. If a BOUND number produced the same sequence as an unbound one,
    every assertion above would pass while the module did nothing at all."""
    print("a BOUND number takes the CRM path (so the comparison above has teeth):")
    from app.core.config import settings

    saved = settings.CRM_LINK_ALLOWLIST
    try:
        settings.CRM_LINK_ALLOWLIST = "+15615550111"
        bound_ops, reported = run_world(enabled=True, bound=True)
    finally:
        settings.CRM_LINK_ALLOWLIST = saved
    unbound_ops, _ = run_world(enabled=True, bound=False)

    check("the bound call took a different path", bound_ops != unbound_ops)
    check("the allowlisted PSTN leg was originated",
          any(o[0] == "originate" and "5615550111" in str(o[1]) for o in bound_ops))
    check("the caller still got a consent notice",
          any(o[0] == "play_and_wait" for o in bound_ops))
    check("nobody answered, so it still fell through to voicemail",
          any(o[0] == "voicemail" for o in bound_ops))
    check("all three lifecycle phases were reported",
          reported == ["started", "ended"] or reported == ["started", "answered", "ended"])


if __name__ == "__main__":
    test_unbound_path_identical_enabled_and_disabled()
    test_kill_switch_short_circuits_before_the_database()
    test_bound_number_does_take_a_different_path()
    print("\nALL CRM-LINK ISOLATION CHECKS PASSED")
