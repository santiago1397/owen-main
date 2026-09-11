"""Who in the CRM is allowed a browser softphone, and for how long. PURE (stdlib only).

The CRM (`ghl-clone`) wants to be one of the phones a bound DID rings: the owner's
requirement is "i should be able to answer from the crm or the other 2 phones, the one
that picks up first, takes the call". `ring.py` already rings `PJSIP/operator-<slug>`
legs alongside the PSTN numbers, so the missing half is purely a credential question —
a CRM user's browser has to be able to REGISTER as one of those operator endpoints.

## The mapping rule, and why there is a roster at all

A CRM user maps to an OWEN operator **by email**, through the same
`telephony.credentials.operator_slug` every other operator path already uses. That is the
owner's decision and it is not re-implemented here: this module imports that function
rather than copying the regex, so the slug a CRM user registers as is by construction the
slug `ring.py` dials and the slug rendered into `asterisk/pjsip.conf`.

What email -> slug does NOT tell you is whether that operator EXISTS. `operator_slug`
is total: it maps any string to something. Minting credentials straight off it would
invent an operator on the fly — the browser would receive a well-formed blob, register
against an endpoint with no `[operator-<slug>]` section in pjsip.conf, and get a SIP 401
with no explanation. Worse, an operator that does not exist can never be rung, so the
user would see "registered" in one place and never receive a call.

So an operator must be **provisioned**, and `CRM_LINK_SOFTPHONE_OPERATORS` is where that
is declared. It mirrors, by hand, the `operator-<slug>` sections in `asterisk/pjsip.conf`
— adding an operator is already a two-file job (pjsip.conf + a reload), and this makes the
missing half a clear 403 at credential time instead of a silent registration failure.

**An EMPTY roster grants NOTHING**, exactly like `CRM_LINK_ALLOWLIST`. The failure mode of
a forgotten or mis-parsed roster has to be "nobody got a softphone", never "anybody did".

## Short-lived, and never longer than the platform's own

`CRM_LINK_SOFTPHONE_TTL_SECONDS` caps both the SIP and the TURN lifetime for this path.
It is a CAP, not a replacement: `capped_ttl` takes the smaller of it and the platform
value, so this endpoint can never hand out a credential that outlives what
`POST /api/telephony/webrtc/credentials` would give the same operator.

The TURN half genuinely self-expires (coturn validates the HMAC and the embedded expiry).
The SIP half is the per-deployment `OPERATOR_SIP_SECRET`, so its `expires_at` is a
re-mint instruction to the client rather than a server-enforced deadline — the same
honest limitation `telephony/credentials.py` records. Keeping the window short means a
blob scraped out of a browser is useless sooner; it does not make it revocable.
"""

from __future__ import annotations

import re

from app.telephony.credentials import operator_endpoint_name, operator_slug

# Comma / semicolon / newline separated, like `config.parse_allowlist`. NOT space —
# for the same reason: a human-written list gets spaces in it, and splitting on
# whitespace turns one entry into two useless ones.
_SEPARATORS = re.compile(r"[,;\n\r]+")

# Never mint anything shorter than this, whatever the configuration says. A 5-second
# credential is not a security control, it is an outage: the browser would spend its whole
# life re-minting and never hold a usable registration.
MIN_TTL_SECONDS = 60

REFUSE_NO_EMAIL = "no CRM user email given"
REFUSE_NO_ROSTER = (
    "no CRM softphone operators are provisioned (set CRM_LINK_SOFTPHONE_OPERATORS)"
)
REFUSE_UNKNOWN_OPERATOR = (
    "that CRM user is not a provisioned OWEN operator "
    "(add them to CRM_LINK_SOFTPHONE_OPERATORS and to asterisk/pjsip.conf)"
)


def parse_roster(raw: str | None) -> frozenset[str]:
    """The provisioned operator SLUGS, from a separated list of emails.

    Slugs rather than emails, because the slug is the identity everything downstream
    actually uses — the PJSIP endpoint name, the TURN username, the ARI dial string. Two
    spellings of one email (`Owen@X.com`, `owen@x.com`) collapse onto one operator here
    rather than becoming two, which is the same collapse `operator_slug` performs.
    """
    out: set[str] = set()
    for part in _SEPARATORS.split(str(raw or "")):
        part = part.strip()
        if not part:
            continue
        slug = operator_slug(part)
        # `operator_slug` degrades an unusable id to "unknown". Rostering that would make
        # every junk email resolve to one shared operator, so it is dropped.
        if slug and slug != "unknown":
            out.add(slug)
    return frozenset(out)


def resolve_operator(email: str | None, roster: frozenset[str]) -> tuple[str, str | None]:
    """`(slug, refusal)` for a CRM user's email. `refusal` is None when they may register.

    Refusals are returned rather than raised so the caller decides the status code, and
    they are distinct strings: "you configured nobody" and "you configured somebody, but
    not this person" are different operational problems and the CRM is a machine caller
    that cannot ask a follow-up question.
    """
    text = str(email or "").strip()
    if not text:
        return "", REFUSE_NO_EMAIL
    slug = operator_slug(text)
    if slug == "unknown":
        return "", REFUSE_NO_EMAIL
    if not roster:
        return slug, REFUSE_NO_ROSTER
    if slug not in roster:
        return slug, REFUSE_UNKNOWN_OPERATOR
    return slug, None


def capped_ttl(cap_seconds: int, platform_seconds: int) -> int:
    """The credential lifetime this path may use: the smaller of the two, floored.

    A cap that could EXCEED the platform's own TTL would make this endpoint the most
    generous door in the building, which is the opposite of what a machine-authenticated
    side entrance should be. Taking the minimum means the CRM path is always the shortest
    lived, whatever either value is set to.
    """
    values = [int(v) for v in (cap_seconds, platform_seconds) if int(v or 0) > 0]
    if not values:
        return MIN_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(values))


def endpoint_for(slug: str) -> str:
    """`operator-<slug>` — the PJSIP endpoint the browser registers as. Reuses the naming
    function so this can never drift from what `ring.py` dials."""
    return operator_endpoint_name(slug)
