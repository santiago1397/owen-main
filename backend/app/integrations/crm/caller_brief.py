"""The CRM's customer brief, in the words a voice agent is given — PURE (stdlib only).

`context_provider.kind: crm_link` (2026-09-24). The CRM (`ghl-clone`) answers
`POST /api/agent-context` with who a caller is and where their job stands; this module turns
that answer into the provider shape owen-voice already consumes (CRM_CONTEXT_SPEC C7):

    {"display_name": "Maria Ruiz", "summary": "<a few sentences>", "facts": {}}

owen-voice then renders it through its unchanged `render_blob`, which puts the name first
("The caller is Maria Ruiz."), then this summary, then its own "use only if relevant, do not
read it back" line, under a "Caller context:" heading — CONTEXT for the model, not instructions.

## What is read, and what is not

Only the keys the CRM's contract names are read, each by name: `known`, `contact.first_name /
last_name`, `opportunity.title / stage`, `next_appointment.starts_at / title`, `last_contact_at`.
The CRM already refuses to send money, notes, email, address or the Checklist; this side does
not trust that either — anything else in the body, whatever it is called, is never looked at,
so a CRM that one day grows a `value_cents` cannot put it in a caller's ear. `facts` is always
empty for the same reason: nothing passes through to the allowlist untouched.

## Times

A visit is spoken in America/New_York, the account's zone — "Tuesday 29 September at 9:00 AM",
"today at 2:30 PM" — never the UTC instant the CRM stores. A UTC time read aloud is three or
four hours wrong to a customer in Bradenton.

## A household can share a phone

The CRM names a caller only when exactly one contact holds that line, but that is still who
the NUMBER belongs to, not proof of who is speaking. The brief says so every time it names
anyone, so the agent confirms before it assumes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ACCOUNT_TZ = ZoneInfo("America/New_York")

# Staff type these; they reach a model's context, so they are flattened to one line and capped.
# Capped short enough that the whole summary stays inside owen-voice's MAX_SUMMARY_CHARS (600)
# and is never cut mid-sentence there.
MAX_TITLE = 60
MAX_NAME = 40

EMPTY: dict = {"display_name": None, "summary": "", "facts": {}}

HOUSEHOLD = ("That is who this number belongs to in our records, and a household can share a "
             "phone: confirm who you are speaking with before using their name or details.")

_SPACE = re.compile(r"\s+")


def _clean(value, cap: int) -> str:
    """One line, trimmed, capped, and never a quote that could close the one around it."""
    text = _SPACE.sub(" ", str(value or "")).strip().replace('"', "'")
    return text[:cap].rstrip()


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The CRM always sends an offset. A naive one would be a guess about the zone, and a
    # guessed time is worse than no time — so it is dropped rather than assumed UTC.
    return dt if dt.tzinfo is not None else None


def _clock(local: datetime) -> str:
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _day(local: datetime, today) -> str:
    text = f"{local:%A} {local.day} {local:%B}"
    if local.year != today.year:
        text += f" {local.year}"
    return text


def spoken_when(value, now: datetime | None = None, *, with_time: bool = True) -> str:
    """An instant as a person says it in Bradenton: "tomorrow, Wednesday 30 September, at
    9:00 AM", "today at 2:30 PM"; without the time, "today" or "on Monday 21 September".
    "" when the value is missing or unusable."""
    dt = _parse(value)
    if dt is None:
        return ""
    local = dt.astimezone(ACCOUNT_TZ)
    today = (now or datetime.now(timezone.utc)).astimezone(ACCOUNT_TZ).date()
    day = _day(local, today)
    if local.date() == today:
        return f"today at {_clock(local)}" if with_time else "today"
    if not with_time:
        return f"on {day}"
    if local.date() == today + timedelta(days=1):
        day = f"tomorrow, {day},"
    return f"{day} at {_clock(local)}"


def to_provider(answer, now: datetime | None = None) -> dict:
    """The CRM's `/api/agent-context` answer -> `{display_name, summary, facts}`.

    Anything but an explicit `known: true` is EMPTY: an unknown caller, a household the CRM
    would not choose between, and a malformed body all mean the agent is told nothing."""
    if not isinstance(answer, dict) or answer.get("known") is not True:
        return dict(EMPTY)
    contact = answer.get("contact") if isinstance(answer.get("contact"), dict) else {}
    name = " ".join(p for p in (_clean(contact.get("first_name"), MAX_NAME),
                                _clean(contact.get("last_name"), MAX_NAME)) if p)
    if not name:
        # A contact with no name gives the agent nothing to confirm against, and a job and a
        # visit described to an unnamed caller is exactly the wrong-household case. Say nothing.
        return dict(EMPTY)

    lines = [HOUSEHOLD]
    opp = answer.get("opportunity") if isinstance(answer.get("opportunity"), dict) else None
    if opp:
        title = _clean(opp.get("title"), MAX_TITLE)
        stage = _clean(opp.get("stage"), MAX_TITLE)
        if title and stage:
            lines.append(f'Their open job "{title}" is at the {stage} stage.')
        elif title:
            lines.append(f'They have an open job, "{title}".')
    visit = (answer.get("next_appointment")
             if isinstance(answer.get("next_appointment"), dict) else None)
    if visit:
        when = spoken_when(visit.get("starts_at"), now)
        title = _clean(visit.get("title"), MAX_TITLE)
        if when and title:
            lines.append(f'Their next visit, "{title}", is {when}.')
        elif when:
            lines.append(f"Their next visit is {when}.")
    last = spoken_when(answer.get("last_contact_at"), now, with_time=False)
    if last:
        lines.append(f"We were last in touch with them {last}.")

    return {"display_name": name, "summary": " ".join(lines), "facts": {}}
