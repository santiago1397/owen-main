"""The CRM's caller brief as Retell dynamic variables — PURE (RETELL-PLAN C2, decision 4).

Retell substitutes `{{name}}` placeholders in the agent's prompt with strings OWEN sends at
registration (`retell_llm_dynamic_variables`). Five are sent, always, all strings:

    customer_known       "yes" | "no"
    customer_first_name  "" when unknown
    customer_brief       the rendered block below
    caller_number        E.164
    dialed_number        the DID the caller rang

`customer_brief` ALWAYS starts with the disclosure rule, known caller or not, so a prompt
that uses `{{customer_brief}}` can never receive facts without the rule above them. The rule
is fixed here, not in Retell's dashboard, because it is the business's rule (decision 4) and
Retell owns only how the agent talks (decision 1).

## What is read

Only the keys C2 names, each by name (`ALLOWED_KEYS`), exactly as `crm/caller_brief.py` does
for owen-voice: anything else the CRM sends — whatever it is called — is never looked at, so a
CRM that one day grows a `value_cents` cannot put money in a caller's ear. The CRM already
withholds money, notes and the Checklist; this side does not trust that either.

## The address

Sent (decision 9, the owner's choice) so the agent can COMPARE what the caller says; the
rule tells it never to read it back. Consequence accepted by the owner: it sits in Retell's
call logs.
"""

from __future__ import annotations

from datetime import datetime

from app.integrations.crm.caller_brief import HOUSEHOLD, _clean, spoken_when

DISCLOSURE_RULE = (
    "HOW TO USE THE FACTS BELOW. They are for understanding the caller, not for reading out. "
    "You may use the caller's first name, and say they have a job with us, straight away. "
    "Share dates, job status, the technician's name or anything from their history ONLY "
    "after the caller has confirmed their street address. Never read the address aloud: ask "
    "the caller to say it, and compare. Never discuss prices, invoices, payments or money of "
    "any kind."
)

UNKNOWN_LINE = ("We have no record of this caller. Treat them as a new customer and do not "
                "guess who they are.")

# C2's limits, re-applied here: the CRM caps them, and this side does not depend on that.
MAX_RECENT_CALLS = 3
MAX_CALL_SUMMARY = 400
MAX_RECENT_TEXTS = 3
MAX_TEXT = 200
MAX_LINE = 120
# A ceiling on the whole block. Retell sends it with every turn's prompt; an unbounded block
# is paid for on every turn of every call (the same reasoning as KNOWLEDGE_MAX_CHARS).
MAX_BRIEF = 3500

ALLOWED_KEYS = frozenset({
    "known", "source", "contact", "opportunity", "next_appointment", "last_contact_at",
    "zuper_job", "recent_calls", "recent_texts", "address",
})

_CHANNEL_WORDS = {"quo": "phone", "zuper_connect": "phone (Zuper)", "ai": "AI agent call"}


def filter_answer(answer) -> dict:
    """The CRM's answer with only the C2 keys kept. `{"known": False}` for anything else."""
    if not isinstance(answer, dict):
        return {"known": False}
    return {k: v for k, v in answer.items() if k in ALLOWED_KEYS}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _known_lines(answer: dict, now: datetime | None) -> tuple[str, list[str]]:
    contact = _dict(answer.get("contact"))
    first = _clean(contact.get("first_name"), 40)
    last = _clean(contact.get("last_name"), 40)
    name = " ".join(p for p in (first, last) if p)
    source = "Zuper" if str(answer.get("source") or "") == "zuper" else "our CRM"
    lines = [f"The caller is {name} (from {source}). {HOUSEHOLD}"]

    address = _clean(answer.get("address"), MAX_LINE)
    if address:
        lines.append(f"Address on file, FOR COMPARISON ONLY, never read it aloud: {address}.")

    opp = _dict(answer.get("opportunity"))
    title, stage = _clean(opp.get("title"), MAX_LINE), _clean(opp.get("stage"), MAX_LINE)
    if title:
        lines.append(f'Open job: "{title}"' + (f", at the {stage} stage." if stage else "."))

    visit = _dict(answer.get("next_appointment"))
    when = spoken_when(visit.get("starts_at"), now)
    if when:
        vt = _clean(visit.get("title"), MAX_LINE)
        lines.append(f"Next visit: {when}" + (f' ("{vt}").' if vt else "."))

    job = _dict(answer.get("zuper_job"))
    if job:
        bits = []
        number = _clean(job.get("job_number"), 40)
        board = _clean(job.get("board"), MAX_LINE)
        status = _clean(job.get("status"), MAX_LINE)
        if status:
            since = spoken_when(job.get("status_since"), now, with_time=False)
            bits.append(f"status {status}" + (f" since {since}" if since else ""))
        tech = _clean(job.get("technician"), 60)
        if tech:
            bits.append(f"technician {tech}")
        sched = spoken_when(job.get("scheduled_start"), now)
        if sched:
            bits.append(f"scheduled {sched}")
        head = "Zuper job" + (f" #{number}" if number else "") + (f" on {board}" if board else "")
        lines.append(head + (": " + "; ".join(bits) if bits else "") + ".")

    last_contact = spoken_when(answer.get("last_contact_at"), now, with_time=False)
    if last_contact:
        lines.append(f"We were last in touch with them {last_contact}.")

    calls = _list(answer.get("recent_calls"))[:MAX_RECENT_CALLS]
    if calls:
        lines.append("Recent calls, newest first:")
        for c in calls:
            at = spoken_when(c.get("at"), now, with_time=False) or "recently"
            channel = _CHANNEL_WORDS.get(str(c.get("channel") or ""), "phone")
            direction = "they called us" if str(c.get("direction") or "").lower() \
                .startswith("in") else "we called them"
            summary = _clean(c.get("summary"), MAX_CALL_SUMMARY)
            lines.append(f"- {at}, {channel}, {direction}: {summary}" if summary
                         else f"- {at}, {channel}, {direction}.")

    texts = _list(answer.get("recent_texts"))[:MAX_RECENT_TEXTS]
    if texts:
        lines.append("Recent texts, newest first:")
        for t in texts:
            at = spoken_when(t.get("at"), now, with_time=False) or "recently"
            who = "they wrote" if str(t.get("direction") or "").lower().startswith("in") \
                else "we wrote"
            text = _clean(t.get("text"), MAX_TEXT)
            if text:
                lines.append(f'- {at}, {who}: "{text}"')
    return first, lines


GREETING_UNKNOWN = "Thank you for calling Dream Team Roofing! How can I help you today?"
GREETING_KNOWN = "Hi {first}, thanks for calling Dream Team Roofing! How can I help you today?"


def render_variables(answer, *, caller_number: str, dialed_number: str,
                     now: datetime | None = None) -> dict[str, str]:
    """The six dynamic variables for one call. Anything but an explicit `known: true` with
    a named contact is UNKNOWN: nothing about anybody, the rule still first."""
    answer = filter_answer(answer)
    known = answer.get("known") is True
    first, lines = ("", [])
    if known:
        first, lines = _known_lines(answer, now)
        if not first and not _clean(_dict(answer.get("contact")).get("last_name"), 40):
            # No name to confirm against: the wrong-household case. Say nothing.
            known, first, lines = False, "", []
    body = "\n".join(lines) if known else UNKNOWN_LINE
    brief = f"{DISCLOSURE_RULE}\n\n{body}"
    if len(brief) > MAX_BRIEF:
        brief = brief[:MAX_BRIEF].rsplit("\n", 1)[0]
    return {
        "customer_known": "yes" if known else "no",
        "customer_first_name": first if known else "",
        # The agent's first words (Retell's begin message is "{{greeting}}"): the caller's
        # first name only when the brief named exactly one customer — decision 4 allows the
        # first name at once; everything else waits for the address check (2026-10-08).
        "greeting": (GREETING_KNOWN.format(first=first) if known and first
                     else GREETING_UNKNOWN),
        "customer_brief": brief,
        "caller_number": str(caller_number or ""),
        "dialed_number": str(dialed_number or ""),
    }
