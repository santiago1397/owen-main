"""Pure SMS-messaging logic for manual outbound + opt-out + status advance (Ticket 10).

Kept import-light (stdlib only) — no sqlalchemy / httpx — so the keyword classification,
opt-out state transition, outbound-gate reasons and forward-only status ranking are all
unit-testable in a bare sandbox. The DB-touching helpers (opt-out upsert, gate enforcement,
outbound-row insert) live in app/services/messages.py; this module is the decision core.
"""

# App-level opt-out keywords (10DLC/CTIA convention). Matched case-insensitively against the
# trimmed, single-word body. STOP wins over START/HELP if a body is ambiguous (safety-first).
STOP_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT"})
START_KEYWORDS = frozenset({"START", "YES", "UNSTOP", "OPTIN"})
HELP_KEYWORDS = frozenset({"HELP", "INFO"})

# Opt-out row state values (absence of a row == never interacted == allowed to send).
OPTED_OUT = "opted_out"
OPTED_IN = "opted_in"


def classify_keyword(body: str | None) -> str | None:
    """Return 'stop' | 'start' | 'help' for an inbound body that is a control keyword, else
    None. The whole (trimmed) body must be the keyword — a keyword embedded in a sentence is
    NOT a command, matching carrier behaviour."""
    if not body:
        return None
    word = body.strip().upper()
    if not word:
        return None
    if word in STOP_KEYWORDS:
        return "stop"
    if word in START_KEYWORDS:
        return "start"
    if word in HELP_KEYWORDS:
        return "help"
    return None


def next_optout_state(current: str | None, keyword: str | None) -> str | None:
    """Fold a classified keyword into the opt-out state. STOP -> opted_out, START -> opted_in;
    HELP / a non-keyword leaves the state unchanged (returns `current`)."""
    if keyword == "stop":
        return OPTED_OUT
    if keyword == "start":
        return OPTED_IN
    return current


def is_opted_out(state: str | None) -> bool:
    """A contact is blocked only when it has an explicit opted_out row."""
    return state == OPTED_OUT


def outbound_block_reason(sms_enabled: bool, sms_campaign_id: str | None) -> str | None:
    """Why (if at all) this NUMBER may not send outbound SMS — the per-number 10DLC gate,
    independent of any per-contact opt-out. None means the number itself is clear to send.
    Used both to refuse the send endpoint and to render the disabled-composer reason."""
    if not sms_enabled:
        return "This number is not enabled for outbound SMS (pending 10DLC registration)."
    if not sms_campaign_id:
        return "This number has no 10DLC campaign assigned yet."
    return None


# Forward-only outbound delivery status ladder. A DLR (message-status webhook) may only ever
# ADVANCE status; a lower- or equal-rank update is ignored (guards out-of-order retries).
# Status may legitimately rest at 'sent' (BulkVS need not emit a terminal DLR). 'failed' /
# 'undelivered' are terminal peers of 'delivered' — once terminal, status never moves again.
OUTBOUND_STATUS_RANK = {
    "queued": 1,
    "sent": 2,
    "delivered": 3,
    "undelivered": 3,
    "failed": 3,
    "blocked": 3,
}


def advance_status(current: str | None, new: str | None) -> str | None:
    """Return the status the row should hold after seeing `new`. Forward-only: keeps `current`
    unless `new` has a strictly higher rank. Unknown `new` values are ignored (keep current)."""
    new_rank = OUTBOUND_STATUS_RANK.get((new or "").lower(), 0)
    cur_rank = OUTBOUND_STATUS_RANK.get((current or "").lower(), 0)
    if new_rank > cur_rank:
        return (new or "").lower()
    return current
