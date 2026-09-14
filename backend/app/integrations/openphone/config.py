"""OpenPhone-mirror configuration kernel — PURE (stdlib only).

Kept import-light for exactly the reason `integrations/crm/config.py` is: the kill switch
and the number selection are what decide whether this module touches a third-party API at
all, so they must be testable in a bare sandbox with no database, no httpx and no pydantic.

Nothing here reads `app.core.config` at import time. `settings_view()` takes the settings
object as an argument, so a test can hand it a plain namespace.

THE KILL SWITCH. `OPENPHONE_MIRROR_ENABLED` defaults to False and is checked by
`mirror_enabled()`, which every entry point into this module calls FIRST. With it off:

  * `worker.build_scheduler` never schedules the poll, so nothing wakes up;
  * `sync.run_once` returns immediately, before constructing a client or opening a session;
  * every route on the `/api/openphone-mirror` router answers 503 before doing any work.

There is a SECOND switch underneath it, and it is not redundant: `OPENPHONE_API_KEY`.
With the key empty, `openphone_client._get` raises rather than firing an unauthenticated
request, so an operator who turns the mirror on in an environment that has no key gets a
logged refusal and not a stream of 401s.

## Why the number list defaults to "all", when CRM_LINK_ALLOWLIST defaults to "none"

They are deliberately opposite, because the failure they guard against is opposite.

`CRM_LINK_ALLOWLIST` governs where a CALL MAY BE PLACED. An empty one must allow nothing:
"no call went out" is a refusal somebody notices and fixes, while "a call went out to an
arbitrary number" is a customer-facing mistake you cannot take back.

This list governs what OWEN READS. An empty one allows everything the account exposes,
because the owner's instruction is "there is one number, mirror it" and a mirror that
silently omits a line is a customer timeline with a hole in it — the exact failure this
feature exists to prevent. The protection against reading too much is the kill switch above
it, which is off by default, plus `exclude`, which always wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_DIGITS = re.compile(r"\D")

# The mirror's window. Everything older than this is not backfilled; nothing is ever
# pruned once mirrored (the owner's decision — see DECISIONS.md, 2026-09-11).
DEFAULT_BACKFILL_DAYS = 30


def digits(value: str | None) -> str:
    """Every digit in `value`, in order. "" for None/blank."""
    return _NON_DIGITS.sub("", str(value or ""))


def match_key(value: str | None) -> str:
    """The comparison key for a phone number: its last ten digits.

    Byte-for-byte the rule in `integrations/crm/config.match_key`, and that duplication is
    deliberate rather than an oversight: this module must not import the CRM link's kernel
    just to compare two numbers, and the rule is four lines. The two are pinned to each
    other by `tests/test_openphone_mirror.py`, which runs the same cases through both.

    Shorter-than-10 input keys on whatever it has, so a typo'd 7-digit entry can only ever
    match another 7-digit entry — never a real DID by accident.
    """
    d = digits(value)
    return d[-10:] if len(d) >= 10 else d


def to_e164(value: str | None) -> str:
    """A participant as Quo requires it (`^\\+[1-9]\\d{1,14}$`), or "" if it cannot be one.

    Quo's List calls / List messages reject anything else with a 400, and an address-book
    entry is whatever a person typed ("(941) 555-0123"). NANP rules, matching
    `crm.config.to_e164`: ten digits are +1; eleven starting with 1 get a +; a number that
    already carried a + keeps its own country code. Anything shorter than ten digits (a
    short code, an extension typo) is not a participant anybody can be asked about.
    """
    raw = str(value or "").strip()
    d = digits(raw)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    if raw.startswith("+") and 10 <= len(d) <= 15 and not d.startswith("0"):
        return "+" + d
    return ""


_LONG_NUMBER = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def redact(text: str | None, limit: int = 300) -> str:
    """Text safe to print or log: every phone-number-shaped run replaced by <number>.

    Used for the errors `preview` prints and the warnings the poll logs. Quo echoes the
    request back in its errors (the 400 on production carried the customer's number in
    the URL), so an error message is customer data until this has run over it.
    """
    return _LONG_NUMBER.sub("<number>", str(text or ""))[:limit]


def parse_numbers(raw: str | None) -> frozenset[str]:
    """Comma / semicolon / newline-separated numbers -> a set of match keys.

    NOT space-separated, for the reason `crm.config.parse_allowlist` spells out at length:
    people write phone numbers with spaces in them, and splitting on whitespace turns one
    destination into two useless fragments.
    """
    parts = re.split(r"[,;\n\r]+", str(raw or ""))
    return frozenset(k for k in (match_key(p) for p in parts) if k)


# --- refusal reasons ----------------------------------------------------------------------
# Strings rather than exceptions, matching `crm/config.py`: the caller decides whether a
# refusal is a 503, a skipped poll or a log line. Every one is designed to be greppable.

REFUSE_KILL_SWITCH = "the OpenPhone mirror is disabled (OPENPHONE_MIRROR_ENABLED=false)"
REFUSE_NO_KEY = "OpenPhone is not configured (OPENPHONE_API_KEY is empty)"
REFUSE_NO_AGENT_KEY = "AGENT_RUNTIME_KEY is unset; the worker could not authenticate back"

# Namespace for the CRM's `dedupe_key`. The CRM stores this verbatim on a UNIQUE column, so
# it is the join key that makes re-ingesting a call a no-op. Namespaced because the column
# is shared with anything else that ever wants idempotent ingest, and two systems minting
# bare provider ids into one unique index would collide on the day their id spaces overlap.
DEDUPE_PREFIX = "openphone"


def dedupe_key(kind: str, external_id: str) -> str:
    """`openphone:call:AC123` / `openphone:message:AC456`.

    Stable across restarts, across a re-backfill and across a worker retry, because it is
    derived from nothing but OpenPhone's own immutable id. That is the whole idempotency
    argument: the same call can be mirrored any number of times and lands on one row.
    """
    return f"{DEDUPE_PREFIX}:{str(kind).strip()}:{str(external_id).strip()}"


@dataclass(frozen=True)
class MirrorSettings:
    """A flat, immutable view of the OPENPHONE_MIRROR_* settings."""

    enabled: bool = False
    api_key_present: bool = False
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    poll_seconds: int = 300
    max_participants: int = 200
    fetch_transcripts: bool = True
    page_limit: int = 50

    def mirrors(self, number: str | None) -> bool:
        """Is this OpenPhone line one we mirror?

        Exclude always wins. An empty `include` means "every line on the account" — see the
        module docstring for why this default is the opposite of the CRM link's.
        """
        key = match_key(number)
        if not key:
            return False
        if key in self.exclude:
            return False
        return True if not self.include else key in self.include

    def refusal(self) -> str | None:
        """The configuration refusal that stops a poll, or None to proceed."""
        if not self.enabled:
            return REFUSE_KILL_SWITCH
        if not self.api_key_present:
            return REFUSE_NO_KEY
        return None


def tick_record(result: dict | None, at_iso: str) -> dict:
    """The heartbeat row for one poll tick, from `sync.run_once`'s result.

    Deliberately lossy. The result carries the mirrored line's number and per-error detail;
    this keeps only what "is the sync alive?" needs — when, whether it ran, which mode, and
    how many errors — so the row can be handed to the CRM without naming anybody. The
    reason is redacted anyway: some refusals quote Quo's own error text.
    """
    result = result if isinstance(result, dict) else {}
    reason = result.get("reason")
    errors = result.get("errors")
    return {
        "at": str(at_iso),
        "ran": bool(result.get("ran")),
        "mode": str(result.get("mode") or "") or None,
        "complete": bool(result.get("complete")) if "complete" in result else None,
        "errors": len(errors) if isinstance(errors, list) else 0,
        "reason": redact(reason, 200) if reason else None,
    }


def settings_view(settings) -> MirrorSettings:
    """Build the view from anything with the right attribute names — the real pydantic
    settings object in production, a plain namespace in a test."""
    return MirrorSettings(
        enabled=bool(getattr(settings, "OPENPHONE_MIRROR_ENABLED", False)),
        api_key_present=bool(getattr(settings, "OPENPHONE_API_KEY", "")),
        include=parse_numbers(getattr(settings, "OPENPHONE_MIRROR_NUMBERS", "")),
        exclude=parse_numbers(getattr(settings, "OPENPHONE_MIRROR_EXCLUDE_NUMBERS", "")),
        backfill_days=int(getattr(settings, "OPENPHONE_MIRROR_BACKFILL_DAYS",
                                  DEFAULT_BACKFILL_DAYS) or DEFAULT_BACKFILL_DAYS),
        poll_seconds=int(getattr(settings, "OPENPHONE_MIRROR_POLL_SECONDS", 300) or 300),
        max_participants=int(
            getattr(settings, "OPENPHONE_MIRROR_MAX_PARTICIPANTS", 200) or 200),
        fetch_transcripts=bool(
            getattr(settings, "OPENPHONE_MIRROR_FETCH_TRANSCRIPTS", True)),
        page_limit=int(getattr(settings, "OPENPHONE_MIRROR_PAGE_LIMIT", 50) or 50),
    )


def current() -> MirrorSettings:
    """The live view. Imports `app.core.config` INSIDE the function so this module stays
    importable in a sandbox with no pydantic — the same trick `crm/config.current` uses."""
    from app.core.config import settings

    return settings_view(settings)


def mirror_enabled() -> bool:
    """The kill switch, on its own. Called first by every entry point."""
    return current().enabled
