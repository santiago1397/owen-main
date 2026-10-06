"""Retell's two public doors: `/api/retell/webhook` and `/api/retell/functions/{name}`.

Driven through FastAPI's TestClient over an in-process ASGI transport (no socket), against
the real router, the real signature check, an in-memory registry and the CRM hop replaced.

  1. **The signature** (RETELL-PLAN decision 20): `v=<ms>,d=hex(HMAC-SHA256(key, raw body +
     ms))`. Good -> handled. Wrong key, tampered body, stale (> 5 min), missing, malformed,
     and a RE-SERIALISED body -> 401 and NOTHING done (counted on spies, not inferred).
     No RETELL_API_KEY -> 503 before anything is read.
  2. **Functions**: run only if the PINNED version toggles the tool on; `transfer` resolves a
     NAME on the version's allowlist (refused name -> nothing happens, agent told so);
     `capture_lead` merges; `request_change` posts C3's exact body to the CRM through the
     crm-link client and records the request; a call that is over says so.
  3. **Webhook**: `call_ended` stores the transcript + real cost once and reports the C4
     fields to the CRM under the call's dedupe key; a Retry writes nothing twice;
     `call_analyzed` reports summary/sentiment/successful; an unknown call is acknowledged
     and ignored; a processing failure releases the claim and answers 500 so Retell retries.

Run: python -m tests.test_retell_http
"""

import json
import time

from tests import retell_guard
from tests.retell_support import (CALLER, DID, KEY, LINKEDID, MemoryRegistry, Patch,
                                  Settings)
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "retell_http")


CALL_ID = "call_abc123"
VERSION_ID = "22222222-2222-2222-2222-222222222222"
VERSION_CONFIG = {
    "engine": "retell", "retell_agent_id": "agent_7f3a",
    "tools": {"transfer": True, "capture_lead": True, "request_change": True},
    "transfer_targets": {"office": {"kind": "operator", "target": "desk"}},
}


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.integrations.retell import api

    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _signed(body: dict | bytes, key: str = KEY, ts: int | None = None) -> tuple[bytes, dict]:
    from app.integrations.retell.signature import header_for

    raw = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
    ts = ts if ts is not None else int(time.time() * 1000)
    return raw, {"X-Retell-Signature": header_for(key, raw, ts),
                 "Content-Type": "application/json"}


def _registry():
    reg = MemoryRegistry()
    reg.rows[CALL_ID] = {
        "retell_call_id": CALL_ID, "linkedid": LINKEDID, "status": "live",
        "agent_name": "Receptionist", "agent_version_id": VERSION_ID,
        "caller_number": CALLER, "dialed_number": DID, "exit_port": None, "exit_data": None,
        "captured": None, "requests": None, "ended_event_at": None, "analyzed_event_at": None,
    }
    return reg


class World:
    """The registry, the version lookup, the CRM hop and the persistence, faked; every
    effect recorded so "nothing was done" is a count."""

    def __init__(self, key=KEY, crm_answer=None):
        self.key = key
        self.reg = _registry()
        self.crm_posts: list[dict] = []
        self.events: list = []
        self.persisted: list = []
        self.crm_answer = crm_answer or {"created": True, "where": "task"}
        self.fail_persist = False

    def __enter__(self):
        from app.agents import spend
        from app.integrations.crm import push as crm_push
        from app.integrations.crm.client import CrmResult
        from app.integrations.retell import functions, registry, webhook

        async def version_config(av):
            return dict(VERSION_CONFIG) if str(av) == VERSION_ID else {}

        async def post_request(body):
            self.crm_posts.append(dict(body))
            return CrmResult(True, 200, "", dict(self.crm_answer))

        async def owen_call_id(linkedid):
            return "owen-call-uuid-1"

        async def persist(snap, call):
            if self.fail_persist:
                raise RuntimeError("database down")
            self.persisted.append((snap["retell_call_id"], call.get("call_id")))

        async def enqueue(facts):
            self.events.append(facts)
            return True

        async def no_alert():
            return None

        self.previous = registry.use(self.reg)
        self.settings = Settings(RETELL_API_KEY=self.key)
        self.settings.__enter__()
        self.patch = Patch((functions, "_version_config", version_config),
                           (functions, "_post_request", post_request),
                           (functions, "_owen_call_id", owen_call_id),
                           (webhook, "persist_ended", persist),
                           (crm_push, "enqueue_call_event", enqueue),
                           (spend, "check_alert", no_alert))
        self.patch.__enter__()
        return self

    def __exit__(self, *exc):
        from app.integrations.retell import registry

        self.patch.__exit__(*exc)
        self.settings.__exit__(*exc)
        registry.use(self.previous)
        return False

    def effects(self):
        row = self.reg.rows[CALL_ID]
        return (len(self.crm_posts), len(self.events), len(self.persisted), row["exit_port"],
                row["captured"], row["ended_event_at"])


FN_BODY = {"name": "transfer", "call": {"call_id": CALL_ID}, "args": {"target": "office"}}


# --- 1. the signature ----------------------------------------------------------------------


def test_the_signature_kernel():
    print("the signature, as a pure function:")
    from app.integrations.retell import signature as sig

    raw = b'{"event":"call_ended","call":{"call_id":"x"}}'
    now = 1_760_000_000_000
    good = sig.header_for(KEY, raw, now)
    check("a genuine signature verifies", sig.verify(raw, good, KEY, now_ms=now) is None)
    check("the hex is HMAC-SHA256(key, body + timestamp)",
          good.split("d=")[1] == __import__("hmac").new(
              KEY.encode(), raw + str(now).encode(), "sha256").hexdigest())
    check("another key does not", sig.verify(raw, good, "other", now_ms=now) == sig.REFUSE_MISMATCH)
    check("a changed body does not",
          sig.verify(raw + b" ", good, KEY, now_ms=now) == sig.REFUSE_MISMATCH)
    check("4m59s old is accepted", sig.verify(raw, good, KEY, now_ms=now + 299_000) is None)
    check("5m01s old is stale", sig.verify(raw, good, KEY, now_ms=now + 301_000) == sig.REFUSE_STALE)
    check("from the future too", sig.verify(raw, good, KEY, now_ms=now - 301_000) == sig.REFUSE_STALE)
    check("missing", sig.verify(raw, None, KEY, now_ms=now) == sig.REFUSE_MISSING)
    check("malformed", sig.verify(raw, "sha256=abc", KEY, now_ms=now) == sig.REFUSE_MALFORMED)
    check("no key configured refuses everything", sig.verify(raw, good, "", now_ms=now) == sig.REFUSE_NO_KEY)
    reserialised = json.dumps(json.loads(raw)).encode()
    check("a RE-SERIALISED body fails (the raw bytes are what is signed)",
          reserialised != raw and sig.verify(reserialised, good, KEY, now_ms=now)
          == sig.REFUSE_MISMATCH)


def test_bad_signatures_are_401_and_do_nothing():
    print("the routes refuse unsigned or mis-signed requests before doing anything:")
    client = _client()
    raw, headers = _signed(FN_BODY)
    cases = {
        "the wrong key": _signed(FN_BODY, key="not-the-key")[1],
        "a stale timestamp": _signed(FN_BODY, ts=int(time.time() * 1000) - 6 * 60 * 1000)[1],
        "no header": {"Content-Type": "application/json"},
        "a malformed header": {"X-Retell-Signature": "nonsense",
                               "Content-Type": "application/json"},
    }
    for label, hdrs in cases.items():
        with World() as w:
            r = client.post("/api/retell/functions/transfer", content=raw, headers=hdrs)
            check(f"{label}: 401", r.status_code == 401)
            check(f"{label}: nothing done", w.effects()[:5] == (0, 0, 0, None, None))
    with World() as w:
        r = client.post("/api/retell/functions/transfer", content=raw + b"\n", headers=headers)
        check("a tampered body: 401, nothing done",
              r.status_code == 401 and w.effects()[:5] == (0, 0, 0, None, None))
        tampered = json.dumps(FN_BODY, indent=2).encode()
        r = client.post("/api/retell/functions/transfer", content=tampered, headers=headers)
        check("the same JSON re-serialised: 401, nothing done",
              r.status_code == 401 and w.effects()[:5] == (0, 0, 0, None, None))
    with World() as w:
        ended = {"event": "call_ended", "call": {"call_id": CALL_ID}}
        body, _h = _signed(ended)
        r = client.post("/api/retell/webhook", content=body,
                        headers=_signed(ended, key="nope")[1])
        check("the webhook too: 401 and nothing claimed",
              r.status_code == 401 and w.effects() == (0, 0, 0, None, None, None))

    with World(key="") as w:
        r = client.post("/api/retell/functions/transfer", content=raw, headers=headers)
        check("no RETELL_API_KEY: 503", r.status_code == 503 and "RETELL_API_KEY" in r.text)
        r = client.post("/api/retell/webhook", content=raw, headers=headers)
        check("...for the webhook too, and nothing done",
              r.status_code == 503 and w.effects()[:5] == (0, 0, 0, None, None))

    with World() as w:
        raw2, h2 = _signed({"name": "send_sms", "call": {"call_id": CALL_ID}, "args": {}})
        r = client.post("/api/retell/functions/send_sms", content=raw2, headers=h2)
        check("a function OWEN does not have: 404", r.status_code == 404)
        raw3, h3 = _signed(dict(FN_BODY, name="end_call"))
        r = client.post("/api/retell/functions/transfer", content=raw3, headers=h3)
        check("a body naming another function than the URL: 400, nothing done",
              r.status_code == 400 and w.effects()[3] is None)


# --- 2. functions --------------------------------------------------------------------------


def _fn(client, name, args):
    raw, headers = _signed({"name": name, "call": {"call_id": CALL_ID}, "args": args})
    return client.post(f"/api/retell/functions/{name}", content=raw, headers=headers)


def test_transfer_enforces_the_pinned_allowlist():
    print("transfer: a NAME on the pinned version's allowlist, or nothing:")
    client = _client()
    with World() as w:
        r = _fn(client, "transfer", {"target": "+19005551234"})
        check("a number is not a destination: 200 with a sentence", r.status_code == 200
              and "not something I can do" in r.json()["result"])
        check("...that names what IS allowed", "office" in r.json()["result"])
        check("...and NOTHING was requested of the call", w.reg.rows[CALL_ID]["exit_port"] is None)

        r = _fn(client, "transfer", {"target": "office"})
        check("an allowlisted name: the agent is told it is happening",
              r.status_code == 200 and "Transferring" in r.json()["result"])
        check("the worker is asked to leave by transfer, to that NAME",
              (w.reg.rows[CALL_ID]["exit_port"], w.reg.rows[CALL_ID]["exit_data"])
              == ("transfer", {"destination": "office"}))

        _fn(client, "transfer", {"target": "office"})
        check("a retry changes nothing (first request wins)",
              w.reg.rows[CALL_ID]["exit_data"] == {"destination": "office"})

    with World() as w:
        r = _fn(client, "end_call", {})
        check("end_call is OFF in this pinned version: refused, nothing requested",
              "not something I can do" in r.json()["result"]
              and w.reg.rows[CALL_ID]["exit_port"] is None)


def test_capture_and_request_change():
    print("capture_lead merges into the call's capture:")
    client = _client()
    with World() as w:
        _fn(client, "capture_lead", {"name": "Maria", "intent": "leak"})
        _fn(client, "capture_lead", {"urgency": "soon", "notes": ""})
        check("merged, blanks dropped", w.reg.rows[CALL_ID]["captured"]
              == {"name": "Maria", "intent": "leak", "urgency": "soon"})

    print("request_change posts C3 to the CRM and records it:")
    with World() as w:
        r = _fn(client, "request_change", {"kind": "reschedule",
                                           "request": "  Move my visit   to Friday  "})
        check("one POST to the CRM", len(w.crm_posts) == 1)
        check("with C3's exact body", w.crm_posts[0] == {
            "caller_number": CALLER, "agent_name": "Receptionist",
            "owen_call_id": "owen-call-uuid-1", "kind": "reschedule",
            "request": "Move my visit to Friday"})
        check("the agent hears it was passed on", r.json()["created"] is True
              and "urgent" in r.json()["result"])
        check("the request is kept for the CRM's ai_call.requests",
              w.reg.rows[CALL_ID]["requests"] == [{"kind": "reschedule",
                                                   "request": "Move my visit to Friday",
                                                   "created": True, "where": "task"}])
        _fn(client, "request_change", {"kind": "teleport", "request": "x" * 1500})
        check("an unknown kind is 'other' and the request is capped at 1000",
              w.crm_posts[1]["kind"] == "other" and len(w.crm_posts[1]["request"]) == 1000)
        r = _fn(client, "request_change", {"kind": "cancel", "request": "   "})
        check("an empty request is not sent", len(w.crm_posts) == 2
              and "Say what the caller wants" in r.json()["result"])

    with World(crm_answer={"created": False, "where": None, "reason": "unknown caller"}) as w:
        r = _fn(client, "request_change", {"kind": "cancel", "request": "Cancel it"})
        check("an unknown caller (created false): the agent is told it was NOT logged",
              r.json()["created"] is False and "could not be passed" in r.json()["result"])

    with World() as w:
        w.reg.rows[CALL_ID]["status"] = "ended"
        r = _fn(client, "request_change", {"kind": "cancel", "request": "Cancel it"})
        check("a call that is over: says so, sends nothing",
              "already ended" in r.json()["result"] and w.crm_posts == [])


# --- 3. the webhook ------------------------------------------------------------------------

ENDED = {"event": "call_ended", "call": {
    "call_id": CALL_ID, "agent_id": "agent_7f3a", "agent_version": 6,
    "disconnection_reason": "agent_hangup", "duration_ms": 95500,
    "call_cost": {"combined_cost": 41.5, "product_costs": []},
    "transcript_object": [{"role": "agent", "content": "Dream Team Roofing, this is Ava."},
                          {"role": "user", "content": "Hi, my roof is leaking."}],
}}
ANALYZED = {"event": "call_analyzed", "call": {
    "call_id": CALL_ID, "call_analysis": {"call_summary": "Leak reported; visit requested.",
                                          "user_sentiment": "Neutral",
                                          "call_successful": True}}}


def _hook(client, body):
    raw, headers = _signed(body)
    return client.post("/api/retell/webhook", content=raw, headers=headers)


def test_call_ended_is_stored_once_and_reported_with_c4_fields():
    print("call_ended: stored once, reported to the CRM with C4's fields:")
    client = _client()
    with World() as w:
        w.reg.rows[CALL_ID]["requests"] = [{"kind": "reschedule", "request": "Friday",
                                            "created": True, "where": "task"}]
        r = _hook(client, ENDED)
        check("200", r.status_code == 200 and r.json() == {"ok": True})
        check("persisted once", w.persisted == [(CALL_ID, CALL_ID)])
        check("one ended event queued for the CRM", len(w.events) == 1)
        facts = w.events[0]
        check("an ENDED call event, answered, for this call",
              (facts.phase, facts.outcome, facts.linkedid, facts.owen_call_id)
              == ("ended", "answered", LINKEDID, "owen-call-uuid-1"))
        check("caller and line", (facts.caller_number, facts.dialed_number) == (CALLER, DID))
        check("the duration from Retell", facts.duration_seconds == 95)
        extra = facts.extra
        check("the SAME dedupe key the runtime's report used (the CRM merges)",
              extra["dedupe_key"] == "owen:call:owen-call-uuid-1:ended")
        ai = extra["ai_call"]
        check("C4: engine, retell_call_id, retell_agent_version",
              (ai["engine"], ai["retell_call_id"], ai["retell_agent_version"])
              == ("retell", CALL_ID, 6))
        check("C4: cost_cents is Retell's combined_cost", ai["cost_cents"] == 41.5)
        check("C4: disconnection_reason", ai["disconnection_reason"] == "agent_hangup")
        check("C4: requests", ai["requests"][0]["kind"] == "reschedule")
        check("the agent's name", ai["agent"] == "Receptionist")
        check("the transcript, speaker-labelled", extra["transcript"]
              == "agent: Dream Team Roofing, this is Ava.\ncaller: Hi, my roof is leaking.")
        row = w.reg.rows[CALL_ID]
        check("the version that answered is kept", row["retell_agent_version"] == 6)

        r = _hook(client, ENDED)
        check("a Retell RETRY: 200 duplicate", r.status_code == 200 and r.json() == {"duplicate": True})
        check("...and nothing stored or reported twice",
              len(w.persisted) == 1 and len(w.events) == 1)

    print("call_analyzed: summary, sentiment, successful:")
    with World() as w:
        r = _hook(client, ANALYZED)
        check("200", r.status_code == 200)
        ai = w.events[0].extra["ai_call"]
        check("C4: summary / sentiment / successful",
              (ai["summary"], ai["sentiment"], ai["successful"])
              == ("Leak reported; visit requested.", "Neutral", True))
        check("no transcript and no duration on the analysis report",
              "transcript" not in w.events[0].extra and w.events[0].duration_seconds is None)
        check("nothing persisted for an analysis", w.persisted == [])
        _hook(client, ANALYZED)
        check("an analysis retry reports nothing twice", len(w.events) == 1)


def test_unknown_calls_failures_and_other_events():
    client = _client()
    print("an unknown call id is acknowledged and ignored:")
    with World() as w:
        other = json.loads(json.dumps(ENDED))
        other["call"]["call_id"] = "call_from_elsewhere"
        r = _hook(client, other)
        check("200 ignored", r.status_code == 200 and "ignored" in r.json())
        check("nothing done", w.persisted == [] and w.events == [])

    print("call_started is acknowledged:")
    with World() as w:
        r = _hook(client, {"event": "call_started", "call": {"call_id": CALL_ID}})
        check("200, nothing done", r.status_code == 200 and w.events == [])

    print("a processing failure releases the claim so Retell's retry can do it:")
    with World() as w:
        w.fail_persist = True
        r = _hook(client, ENDED)
        check("500 so Retell retries", r.status_code == 500)
        check("the claim was released", w.reg.rows[CALL_ID]["ended_event_at"] is None)
        check("nothing reported", w.events == [])
        w.fail_persist = False
        r = _hook(client, ENDED)
        check("the retry is processed", r.status_code == 200 and len(w.events) == 1)


def test_the_webhook_pure_parts():
    print("transcript and cost parsing:")
    from app.integrations.retell import webhook

    check("roles map to agent/caller", webhook.segments_from(ENDED["call"])[1]
          == {"speaker": "caller", "text": "Hi, my roof is leaking."})
    check("only a flat transcript: kept whole, not guessed into speakers",
          webhook.segments_from({"transcript": "Agent: hi\nUser: hey"})
          == [{"speaker": "transcript", "text": "Agent: hi\nUser: hey"}])
    check("no cost is None, not zero", webhook.cost_cents({}) is None)
    check("a non-number cost is None", webhook.cost_cents({"call_cost": {"combined_cost": "x"}}) is None)


if __name__ == "__main__":
    test_the_signature_kernel()
    test_bad_signatures_are_401_and_do_nothing()
    test_transfer_enforces_the_pinned_allowlist()
    test_capture_and_request_change()
    test_call_ended_is_stored_once_and_reported_with_c4_fields()
    test_unknown_calls_failures_and_other_events()
    test_the_webhook_pure_parts()
    retell_guard.assert_untouched()
    print("\nALL RETELL HTTP CHECKS PASSED")
