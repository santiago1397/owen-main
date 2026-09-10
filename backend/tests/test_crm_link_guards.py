"""The guards that make it safe to iterate a CRM link next to a live phone system.

Three things are asserted here, and each of them is the difference between a bug and a
phone call to a stranger:

  1. **The hard destination allowlist.** While this module is new, a real call or text may
     only go to a number on `CRM_LINK_ALLOWLIST`. Everything else is refused and logged.
     An EMPTY allowlist allows NOTHING — the failure mode of a mis-parsed or forgotten
     allowlist has to be "no call went out".
  2. **The kill switch.** With `CRM_LINK_ENABLED` false, the outbound-call and SMS endpoints
     refuse before they do anything at all.
  3. **No answer falls through to voicemail**, exactly as the existing unassigned-DID
     handler does — the caller never gets dead air.

Run: python -m tests.test_crm_link_guards
"""

import asyncio

DIALED = "+15615550200"
CALLER = "+15615559999"
ALLOWED = "+15615550111"
NOT_ALLOWED = "+13055557777"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_link_guards failed at: {name}")


def _cfg(**kw):
    """A CrmLinkSettings built straight from the pure kernel — no app settings, no database."""
    from app.integrations.crm import config as c

    base = dict(enabled=True, allowlist=c.parse_allowlist(f"{ALLOWED}, +1 561-555-0122"),
                sms_enabled=False, token="ghl_pat_test")
    base.update(kw)
    return c.CrmLinkSettings(**base)


# --- 1. the allowlist -----------------------------------------------------------------------

def test_allowlist_refuses_anything_not_on_it():
    print("only an allowlisted destination may be dialled or texted:")
    from app.integrations.crm import config as c

    cfg = _cfg()
    check("an allowlisted number is permitted", cfg.allows(ALLOWED))
    check("an unlisted number is refused", not cfg.allows(NOT_ALLOWED))
    check("and says why", cfg.destination_refusal(NOT_ALLOWED) == c.REFUSE_NOT_ALLOWLISTED)
    check("an empty destination is refused",
          cfg.destination_refusal("") == c.REFUSE_NO_DESTINATION)


def test_an_empty_allowlist_allows_nothing():
    print("an EMPTY allowlist allows nothing at all:")
    cfg = _cfg(allowlist=frozenset())
    check("the allowlisted number is now refused too", not cfg.allows(ALLOWED))
    check("so is everything else", not cfg.allows(NOT_ALLOWED))


def test_allowlist_matching_ignores_formatting_but_not_identity():
    print("formatting does not matter; the number does:")
    cfg = _cfg()
    for written in ("+15615550111", "15615550111", "5615550111",
                    "(561) 555-0111", "561-555-0111", "+1 561 555 0111"):
        check(f"{written!r} matches the allowlisted destination", cfg.allows(written))
    check("a different number with the same last four does NOT match",
          not cfg.allows("+15619990111"))
    # A phone number containing spaces must survive allowlist parsing intact. Splitting on
    # whitespace turned `+1 561-555-0122` into the useless keys "1" and "5615550122", so the
    # destination the operator meant to allow was silently absent.
    check("a space-separated entry in the allowlist still parses as ONE destination",
          cfg.allows("+15615550122"))


def test_pstn_ring_legs_are_filtered_and_capped():
    print("a ring group's PSTN legs are filtered by the allowlist, then capped:")
    cfg = _cfg()
    allowed, refused = cfg.filter_pstn([ALLOWED, NOT_ALLOWED, "+15615550122"])
    check("the unlisted destination is dropped", NOT_ALLOWED not in allowed)
    check("and is reported with a reason", [n for n, _r in refused] == [NOT_ALLOWED])
    check("both allowlisted destinations survive", len(allowed) == 2)

    # A refused destination must not consume one of the two slots.
    allowed2, refused2 = cfg.filter_pstn([NOT_ALLOWED, ALLOWED, "+15615550122"])
    check("a refusal does not eat a leg slot", len(allowed2) == 2)
    check("the cap is enforced on the allowed ones",
          len(cfg.filter_pstn([ALLOWED, "+15615550122", ALLOWED])[0]) == 2)
    check("everything past the cap is reported, not silently dropped",
          len(refused2) == 1)


# --- 2. the kill switch on the CRM-facing endpoints ------------------------------------------

def test_outbound_call_and_sms_refuse_while_the_kill_switch_is_off():
    """The endpoints raise 503 from `_require_enabled` before they read anything. Asserted
    against the real dependency rather than a copy of its logic."""
    print("outbound call and SMS refuse entirely with the kill switch off:")
    from fastapi import HTTPException

    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    saved = settings.CRM_LINK_ENABLED
    try:
        settings.CRM_LINK_ENABLED = False
        raised = None
        try:
            crm_api._require_enabled()
        except HTTPException as exc:
            raised = exc
        check("disabled: the shared gate raises", raised is not None)
        check("disabled: it is a 503", raised is not None and raised.status_code == 503)
        check("disabled: it names the switch",
              raised is not None and "CRM_LINK_ENABLED" in str(raised.detail))

        settings.CRM_LINK_ENABLED = True
        cfg = crm_api._require_enabled()
        check("enabled: the gate lets the request through", cfg.enabled is True)
    finally:
        settings.CRM_LINK_ENABLED = saved


def test_sms_stays_dark_even_with_the_link_on_and_the_number_allowlisted():
    """SMS has its OWN switch on top of the kill switch, because the DID's 10DLC campaign is
    submitted and not approved. An allowlisted destination is still refused."""
    print("SMS is dark on its own switch, above the allowlist:")
    from app.integrations.crm import config as c

    dark = _cfg(sms_enabled=False)
    check("an allowlisted number is still refused for SMS",
          dark.sms_refusal(ALLOWED) == c.REFUSE_SMS_DARK)
    check("the same number is fine for a CALL", dark.allows(ALLOWED))

    lit = _cfg(sms_enabled=True)
    check("with the SMS switch on, the allowlisted number is permitted",
          lit.sms_refusal(ALLOWED) is None)
    check("but an unlisted number is STILL refused",
          lit.sms_refusal(NOT_ALLOWED) == c.REFUSE_NOT_ALLOWLISTED)

    off = _cfg(enabled=False, sms_enabled=True)
    check("the global kill switch outranks the SMS switch",
          off.sms_refusal(ALLOWED) == c.REFUSE_KILL_SWITCH)


def test_delivery_refuses_without_a_token():
    print("no CRM token means nothing is queued for delivery:")
    from app.integrations.crm import config as c

    check("no token -> refused", _cfg(token="").delivery_refusal() == c.REFUSE_NO_TOKEN)
    check("disabled -> refused first",
          _cfg(enabled=False, token="").delivery_refusal() == c.REFUSE_KILL_SWITCH)
    check("enabled with a token -> allowed", _cfg().delivery_refusal() is None)


# --- 3. no answer falls through to voicemail --------------------------------------------------

class HandlerAri:
    """Enough of an ARI client for the bound-DID handler, recording the op sequence."""

    def __init__(self, *, answered=False):
        self.ops = []
        self._answered = answered

    async def answer(self, channel_id):
        self.ops.append("answer")

    async def play_and_wait(self, channel_id, media, *, timeout_s=30.0):
        self.ops.append("consent")

    async def available_operators(self):
        return ["PJSIP/operator-desk-x.com"]

    async def ring_start(self, channel_id):
        self.ops.append("ring_start")

    async def ring_stop(self, channel_id):
        self.ops.append("ring_stop")

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        self.ops.append("voicemail")

    async def hangup(self, channel_id):
        self.ops.append("hangup")


def _run_handler(*, ring_port, winner=None):
    """Drive the real handler with the ring group stubbed to a known outcome."""
    from app.core.config import settings
    from app.integrations.crm import handler as crm_handler
    from app.integrations.crm import push as crm_push
    from app.integrations.crm import ring as crm_ring
    from app.integrations.crm.binding import CrmBinding

    ari = HandlerAri()
    binding = CrmBinding(
        link_id="l1", number_id="n1", phone_number=DIALED, friendly_name="Bound",
        campaign_id=None, ring_operators=True, operator_ids=[],
        pstn_numbers=[ALLOWED], ring_timeout_seconds=5,
        crm_base_url="http://crm:8000", crm_token="ghl_pat_test",
    )
    reported = []

    async def fake_ring(_ari, _chan, _legs, *, timeout_s, record_name=None):
        result = crm_ring.RingResult(port=ring_port)
        if winner is not None:
            result.winner = crm_ring.RingLeg(
                kind=winner[0], destination=winner[1], endpoint="x", caller_id=None)
        return result

    async def fake_report(**kw):
        reported.append((kw.get("phase"), kw.get("outcome"), kw.get("winning_destination")))
        return True

    saved = (crm_ring.hybrid_ring_and_bridge, crm_push.report_call_phase,
             settings.CRM_LINK_ALLOWLIST)
    try:
        settings.CRM_LINK_ALLOWLIST = ALLOWED
        crm_handler.ring.hybrid_ring_and_bridge = fake_ring
        crm_handler.push.report_call_phase = fake_report
        asyncio.run(crm_handler.handle_bound_inbound(
            ari, "chan-1", "lid-1", DIALED, CALLER, binding))
    finally:
        (crm_ring.hybrid_ring_and_bridge, crm_push.report_call_phase,
         settings.CRM_LINK_ALLOWLIST) = saved
        crm_handler.ring.hybrid_ring_and_bridge = saved[0]
        crm_handler.push.report_call_phase = saved[1]
    return ari.ops, reported


def test_no_answer_falls_through_to_voicemail():
    print("nobody answers -> voicemail, exactly as the existing default handler does:")
    ops, reported = _run_handler(ring_port="noanswer")
    check("the caller was answered", ops[0] == "answer")
    check("the consent notice played first", ops[1] == "consent")
    check("the group rang", "ring_start" in ops and "ring_stop" in ops)
    check("and it ended in voicemail, not dead air", "voicemail" in ops)
    check("the terminal event says voicemail",
          ("ended", "voicemail", None) in reported)


def test_a_failed_ring_also_reaches_voicemail():
    """A rejected bridge or an ARI error must not leave the caller on dead air either."""
    print("a FAILED ring also reaches voicemail:")
    ops, reported = _run_handler(ring_port="failed")
    check("voicemail was still taken", "voicemail" in ops)
    check("the terminal event was still reported",
          any(p == "ended" for p, _o, _w in reported))


def test_an_answered_call_does_not_get_a_voicemail_greeting():
    print("an ANSWERED call is never played the voicemail greeting:")
    ops, reported = _run_handler(ring_port="answered", winner=("pstn", ALLOWED))
    check("no voicemail", "voicemail" not in ops)
    check("the call was ended after the bridge", ops[-1] == "hangup")
    check("the winning destination was reported",
          ("answered", "answered", ALLOWED) in reported)
    check("and so was the terminal event",
          ("ended", "answered", ALLOWED) in reported)


if __name__ == "__main__":
    test_allowlist_refuses_anything_not_on_it()
    test_an_empty_allowlist_allows_nothing()
    test_allowlist_matching_ignores_formatting_but_not_identity()
    test_pstn_ring_legs_are_filtered_and_capped()
    test_outbound_call_and_sms_refuse_while_the_kill_switch_is_off()
    test_sms_stays_dark_even_with_the_link_on_and_the_number_allowlisted()
    test_delivery_refuses_without_a_token()
    test_no_answer_falls_through_to_voicemail()
    test_a_failed_ring_also_reaches_voicemail()
    test_an_answered_call_does_not_get_a_voicemail_greeting()
    print("\nALL CRM-LINK GUARD CHECKS PASSED")
