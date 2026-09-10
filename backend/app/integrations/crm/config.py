"""CRM-link configuration kernel — PURE (stdlib only).

Kept import-light for the same reason `app/services/sms.py` and `app/telephony/outbound.py`
are: the kill switch, the destination allowlist and the phone normalisation are the parts
that decide whether a real call is placed or a real text is sent, so they must be testable
in a bare sandbox with no database, no httpx and no pydantic.

Nothing here reads `app.core.config` at import time. `settings_view()` takes the settings
object as an argument, so a test can hand it a plain namespace.

THE KILL SWITCH. `CRM_LINK_ENABLED` defaults to False and is checked by `link_enabled()`,
which every entry point into this module calls FIRST. With it off:
  * `hook.handle_bound_inbound` returns False before touching the database, so the existing
    unassigned-DID default handler runs exactly as it does today;
  * every route on the `/api/crm-link` router answers 503 before doing any work;
  * `push.enqueue_call_event` enqueues nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- phone normalisation ------------------------------------------------------------------
# The allowlist is compared on the LAST 10 DIGITS, so "+15615551234", "15615551234",
# "(561) 555-1234" and "561-555-1234" are one destination. Matching on the raw string was
# rejected: an operator typing a number the way a human writes it would silently fall off
# the allowlist, and "silently not on the allowlist" is a refusal, not an outage — the kind
# of failure nobody notices until a call does not go out.

_NON_DIGITS = re.compile(r"\D")


def digits(value: str | None) -> str:
    """Every digit in `value`, in order. "" for None/blank."""
    return _NON_DIGITS.sub("", str(value or ""))


def match_key(value: str | None) -> str:
    """The comparison key for a phone number: its last 10 digits.

    Shorter-than-10 input keys on whatever it has, so a typo'd 7-digit entry can only ever
    match another 7-digit entry — never a real DID by accident.
    """
    d = digits(value)
    return d[-10:] if len(d) >= 10 else d


def to_e164(value: str | None) -> str:
    """Best-effort NANP E.164. Mirrors `providers.bulkvs_client._to_e164`'s rules so a
    number entered in either place normalises the same way; non-NANP input is returned
    digits-only with a '+', never dropped."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    d = digits(raw)
    if not d:
        return ""
    if len(d) == 10:
        return f"+1{d}"
    if len(d) == 11 and d.startswith("1"):
        return f"+{d}"
    return f"+{d}"


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Comma / semicolon / newline-separated destinations -> a set of match keys.

    NOT space-separated, deliberately. People write phone numbers with spaces in them —
    `+1 561-555-0111` is the most ordinary rendering there is — and splitting on whitespace
    turns that single destination into the two useless keys "1" and "5615550111". The
    entry the operator meant to allow would then be silently absent, and a call they
    expected to go out would not. Commas separate; spaces are part of a number.

    An EMPTY allowlist allows NOTHING. That is the whole point of the guard: while this
    module is new, the failure mode of a mis-parsed or unset allowlist must be "no call
    went out", never "a call went out to an arbitrary number".
    """
    parts = re.split(r"[,;\n\r]+", str(raw or ""))
    return frozenset(k for k in (match_key(p) for p in parts) if k)


# --- refusal reasons ----------------------------------------------------------------------
# Returned as strings rather than raised, so the caller decides whether a refusal is a 4xx,
# a skipped ring leg or a log line. Every one of these is designed to be greppable.

REFUSE_KILL_SWITCH = "CRM link is disabled (CRM_LINK_ENABLED=false)"
REFUSE_NOT_ALLOWLISTED = "destination is not on CRM_LINK_ALLOWLIST"
REFUSE_NO_DESTINATION = "no destination given"
REFUSE_SMS_DARK = "CRM-link SMS is dark (CRM_LINK_SMS_ENABLED=false)"
REFUSE_NO_TOKEN = "no CRM token configured (CRM_LINK_TOKEN)"
REFUSE_NOT_BOUND = "that number is not bound to the CRM"


@dataclass(frozen=True)
class CrmLinkSettings:
    """The env half of the configuration, resolved once and passed around.

    The DB half (which DID is bound, to what it rings) is `binding.CrmLinkBinding`. Split
    deliberately: secrets and the kill switch belong in env and change with a deploy, while
    "which number is linked" is data the owner changes without one.
    """

    enabled: bool = False
    base_url: str = ""
    token: str = ""
    allowlist: frozenset[str] = field(default_factory=frozenset)
    sms_enabled: bool = False
    max_pstn_legs: int = 2
    ring_timeout_seconds: int = 25
    http_timeout_seconds: float = 15.0

    # --- the guards -----------------------------------------------------------------------

    def destination_refusal(self, number: str | None) -> str | None:
        """Why this destination may NOT be dialled or texted, or None if it may.

        Order matters: the kill switch is reported before the allowlist so a disabled module
        never leaks which numbers are on the list.
        """
        if not self.enabled:
            return REFUSE_KILL_SWITCH
        key = match_key(number)
        if not key:
            return REFUSE_NO_DESTINATION
        if key not in self.allowlist:
            return REFUSE_NOT_ALLOWLISTED
        return None

    def allows(self, number: str | None) -> bool:
        return self.destination_refusal(number) is None

    def sms_refusal(self, number: str | None) -> str | None:
        """Why an SMS to `number` may not be sent. The module's OWN gate only — the
        per-number 10DLC gate (`numbers.sms_enabled` / `sms_campaign_id`) and the
        per-contact opt-out are enforced separately by the platform's existing
        `services.sms.outbound_block_reason` / opt-out check, which this never replaces."""
        if not self.enabled:
            return REFUSE_KILL_SWITCH
        if not self.sms_enabled:
            return REFUSE_SMS_DARK
        return self.destination_refusal(number)

    def delivery_refusal(self) -> str | None:
        """Why an event cannot be delivered to the CRM right now."""
        if not self.enabled:
            return REFUSE_KILL_SWITCH
        if not self.token:
            return REFUSE_NO_TOKEN
        return None

    def filter_pstn(self, numbers) -> tuple[list[str], list[tuple[str, str]]]:
        """Split candidate PSTN ring destinations into (allowed, refused).

        `refused` carries (number, reason) so the caller can log WHY a leg was dropped —
        a ring group that quietly shrinks from three legs to one is exactly the sort of
        thing that is only ever noticed when somebody misses a call.

        Capped at `max_pstn_legs` AFTER filtering, so a refused destination does not consume
        one of the two slots.
        """
        allowed: list[str] = []
        refused: list[tuple[str, str]] = []
        for raw in numbers or []:
            num = str(raw or "").strip()
            if not num:
                continue
            reason = self.destination_refusal(num)
            if reason:
                refused.append((num, reason))
            elif len(allowed) < self.max_pstn_legs:
                allowed.append(to_e164(num))
            else:
                refused.append((num, f"more than {self.max_pstn_legs} PSTN legs configured"))
        return allowed, refused


def settings_view(settings) -> CrmLinkSettings:
    """Project the app's `Settings` object onto the pure kernel above.

    Duck-typed with `getattr` defaults so this module stays importable — and testable —
    against a settings object that predates these fields.
    """
    return CrmLinkSettings(
        enabled=bool(getattr(settings, "CRM_LINK_ENABLED", False)),
        base_url=str(getattr(settings, "CRM_LINK_BASE_URL", "") or "").rstrip("/"),
        token=str(getattr(settings, "CRM_LINK_TOKEN", "") or ""),
        allowlist=parse_allowlist(getattr(settings, "CRM_LINK_ALLOWLIST", "")),
        sms_enabled=bool(getattr(settings, "CRM_LINK_SMS_ENABLED", False)),
        max_pstn_legs=int(getattr(settings, "CRM_LINK_MAX_PSTN_LEGS", 2) or 2),
        ring_timeout_seconds=int(getattr(settings, "CRM_LINK_RING_TIMEOUT_SECONDS", 25) or 25),
        http_timeout_seconds=float(
            getattr(settings, "CRM_LINK_HTTP_TIMEOUT_SECONDS", 15.0) or 15.0
        ),
    )


def current() -> CrmLinkSettings:
    """The live configuration. Imports `app.core.config` LAZILY so this module keeps its
    stdlib-only import profile for the pure unit tests."""
    from app.core.config import settings

    return settings_view(settings)


def link_enabled() -> bool:
    """THE kill switch. Every entry point into this module calls this first.

    Defensive: if reading configuration raises for any reason at all, the answer is False.
    A CRM integration that fails open on a live phone system is not a CRM integration, it
    is an outage with a feature name.
    """
    try:
        from app.core.config import settings

        return bool(getattr(settings, "CRM_LINK_ENABLED", False))
    except Exception:  # noqa: BLE001 - unreadable config => stay off
        return False
