"""Call ownership — who is driving a live call (AI_AGENT_SPEC D4).

THE INVARIANT this exists to make structural rather than remembered:

    Once a call is HUMAN-OWNED, no automated path may hang up, play, record or bridge
    that channel again.

Without it, take-over is a latent bug rather than a feature. `_h_ai_agent` blocks for the
whole conversation holding the caller's entry channel, and `ai_agent` is not in
TERMINAL_TYPES — so when the agent session ends because a supervisor seized the call, the
interpreter resolves the port (interpreter.py:365-391), finds it unwired, and routes to
`default_fallback`. That plays a voicemail greeting at a caller who is mid-sentence with a
human operator, or, with no fallback configured, calls `_safe_hangup()` on the channel and
hangs up on the caller the operator just rescued.

That is the same failure class this codebase has already paid for twice — outbound legs
receiving the voicemail greeting (asterisk_consumer.py:120-122) and the play/dial race that
bridged over a mid-sentence consent notice. Same root cause every time: a control path
unaware that another actor owns the channel.

PURE + stdlib only (like app/flows/dtmf.py, which this mirrors): in-memory, per-process,
per-call. A worker restart drops the RTP anyway, so there is nothing worth persisting.

Ownership is tracked by BOTH linkedid and channel id. Callers reason in linkedids (a call);
the ARI client only ever sees channel ids, and it is the ARI client that has to enforce the
rule — so the guard must be answerable from a channel id alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("telephony.ownership")


@dataclass
class Ownership:
    linkedid: str
    owner: str                      # the operator who seized the call (user email)
    at: float = field(default_factory=time.monotonic)
    channels: set = field(default_factory=set)
    reason: str = "takeover"


# linkedid -> Ownership
_calls: dict[str, Ownership] = {}
# channel id -> linkedid, so the ARI guard can answer from a channel alone
_channels: dict[str, str] = {}


def claim(linkedid: str, owner: str, *, channels=(), reason: str = "takeover") -> bool:
    """Mark a call human-owned. Returns False if someone already owns it — two supervisors
    seizing the same call is a race worth losing loudly rather than resolving silently."""
    if not linkedid:
        return False
    existing = _calls.get(linkedid)
    if existing is not None and existing.owner != owner:
        logger.warning("ownership: %s already owned by %s; %s refused",
                       linkedid, existing.owner, owner)
        return False
    own = existing or Ownership(linkedid=linkedid, owner=owner, reason=reason)
    for ch in channels:
        if ch:
            own.channels.add(str(ch))
            _channels[str(ch)] = linkedid
    _calls[linkedid] = own
    logger.info("ownership: %s claimed by %s (%d channel(s), %s)",
                linkedid, owner, len(own.channels), reason)
    return True


def add_channel(linkedid: str, channel_id: str) -> None:
    """Attach another channel to an owned call — e.g. the operator's own leg once bridged."""
    own = _calls.get(linkedid)
    if own is None or not channel_id:
        return
    own.channels.add(str(channel_id))
    _channels[str(channel_id)] = linkedid


def release(linkedid: str) -> None:
    own = _calls.pop(linkedid, None)
    if own is None:
        return
    for ch in own.channels:
        _channels.pop(ch, None)
    logger.info("ownership: %s released (was %s)", linkedid, own.owner)


def owner_of(linkedid: str) -> Optional[str]:
    own = _calls.get(linkedid)
    return own.owner if own else None


def is_owned(linkedid: str) -> bool:
    return linkedid in _calls


def is_channel_owned(channel_id: str) -> bool:
    """The question the ARI client asks before every mutating operation."""
    lid = _channels.get(str(channel_id))
    return lid is not None and lid in _calls


def linkedid_of_channel(channel_id: str) -> Optional[str]:
    return _channels.get(str(channel_id))


def snapshot() -> list:
    """Everything currently human-owned, for the API and for debugging."""
    now = time.monotonic()
    return [
        {
            "linkedid": o.linkedid,
            "owner": o.owner,
            "reason": o.reason,
            "held_seconds": round(now - o.at, 1),
            "channels": sorted(o.channels),
        }
        for o in _calls.values()
    ]


def clear() -> None:
    """Tests only."""
    _calls.clear()
    _channels.clear()
