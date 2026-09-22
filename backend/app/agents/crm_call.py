"""What the CRM is told about a call the AI agent answered.

Phase 1 of the CRM's voice-agent amendment (ghl-clone DECISIONS.md, 2026-09-22). An agent
answers a customer on the company's behalf and, until this exists, the CRM shows nothing
at all: not who answered, not what was said, not what the agent wrote down. A dispatcher
reviewing a supervised agent has nothing to review.

It reuses the path that already works — `CallEventFacts` -> `crm_report` job -> the
app-side adapter -> `POST /api/events` — rather than opening a second door to the CRM.
Three fields ride in `facts.extra` and are unpacked by `events.to_crm_event`:

    transcript   the speaker-labelled text, the same string `transcriptions.text` holds
    ai_call      {agent, version, outcome, captured, campaign} — the CRM's `ai_call` column
    dedupe_key   so a RETRY cannot write a second call row on the customer's thread

The capture travels as data, not as a customer record. The CRM creates no contact and no
deal from it: promoting it is a person's decision while the agent is supervised.

Pure and stdlib-only, so it is testable in the sandbox where flows/runtime.py (httpx,
sqlalchemy) cannot even be imported.
"""
from __future__ import annotations

from app.agents.capture import normalise_capture

# How the agent's exit port reads on a CRM thread. The graph's vocabulary is owen-main's;
# the CRM shows it to a dispatcher, and `complete` means nothing to one.
OUTCOME_FOR_CRM = {
    "end_call": "end_call",
    "complete": "end_call",
    "transfer": "transfer",
    "transferred": "transfer",
    "default": "default",
    "failed": "failed",
}


def transcript_text(segments) -> str:
    """The speaker-labelled transcript, exactly as `_persist_agent_output` stores it.

    One formatting, two destinations: a transcript that read differently in the CRM than in
    OWEN would send someone hunting for a second recording of the same call.
    """
    if not isinstance(segments, list):
        return ""
    return "\n".join(
        "%s: %s" % (t.get("speaker", "agent"), t.get("text", ""))
        for t in segments if isinstance(t, dict)
    )


def dedupe_key(owen_call_id: str, phase: str = "ended") -> str:
    """The idempotency key for one call's one report.

    Keyed on the CALL, not on the attempt: `handle_crm_report` retries a delivery five
    times with backoff, and a delivery that timed out AFTER the CRM inserted the row is
    indistinguishable here from one that never arrived. Without this, the customer's thread
    grows a second identical call — and with the CRM's automations re-armed, a second text.
    """
    return "owen:call:%s:%s" % (owen_call_id or "unknown", phase)


def ai_call_record(*, agent_name: str, version, outcome: str, captured,
                   campaign: str = "") -> dict:
    """The `ai_call` object the CRM stores on the event.

    Only what is actually known goes in. An empty capture is omitted rather than sent as
    `{}`: the CRM merges these records across the two reports of one call, and an empty
    value would occupy the key and block the real capture when it arrives.
    """
    record: dict = {}
    if agent_name:
        record["agent"] = agent_name
    if version not in (None, ""):
        record["version"] = version
    if outcome:
        record["outcome"] = OUTCOME_FOR_CRM.get(outcome, outcome)
    if campaign:
        record["campaign"] = campaign
    if isinstance(captured, dict) and captured:
        fields = normalise_capture(captured)
        # `normalise_capture` splits the shared core out from everything else; the CRM
        # renders a flat list, so the extras are folded back in beside the core keys.
        flat = {k: v for k, v in fields.items() if k != "extra"}
        flat.update(fields.get("extra") or {})
        if flat:
            record["captured"] = flat
    return record


def report_extra(*, agent_name: str, version, outcome: str, data: dict,
                 campaign: str = "", owen_call_id: str = "") -> dict:
    """The `extra` blob for `CallEventFacts`, ready for `to_crm_event` to unpack.

    Returns {} when there is nothing worth saying — an agent that never ran leaves the
    CRM's `ai_call` column NULL rather than claiming an empty conversation happened.
    """
    data = data or {}
    record = ai_call_record(agent_name=agent_name, version=version, outcome=outcome,
                            captured=data.get("captured"), campaign=campaign)
    text = transcript_text(data.get("transcript"))
    if not record and not text:
        return {}
    extra: dict = {"ai_call": record} if record else {}
    if text:
        extra["transcript"] = text
    if owen_call_id:
        extra["dedupe_key"] = dedupe_key(owen_call_id)
    return extra
