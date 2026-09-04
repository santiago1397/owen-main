"""Caller-context kernel — PURE, stdlib only (CRM_CONTEXT_SPEC C3/C4/C7).

Identity matching, the field allowlist and the rendered blob all live here, away from
sqlalchemy and httpx, so the two rules that actually matter are testable in a bare sandbox:

  C3  who the caller is — and, just as importantly, when we are NOT sure enough to say a name
  C4  what may be injected — everything here ends up in the model's context AND in a
      `transcriptions` row that `api/ai/content.py` serves to any key holding `content`

Rendering is deterministic and capped. The blob is prepended to the system prompt on every
turn of the call, so an unbounded CRM response would be re-billed on each one.
"""

from __future__ import annotations

import re

# The blob is re-sent every turn. A CRM that returns an essay would be paid for repeatedly,
# so it is truncated rather than trusted.
MAX_SUMMARY_CHARS = 600
MAX_FACT_CHARS = 120
MAX_FACTS = 12


def normalise_phone(number) -> str:
    """A comparable form of a phone number: digits only, last 10 (C3).

    Deliberately NOT fuzzy. Last-10 collapses +1 / 1 / bare-10 and any formatting, which is
    the whole realistic variation in NANP data, and stops there. Matching a spouse's number or
    a shared household line was rejected outright: greeting the wrong person by name is far
    worse than not greeting them at all.
    """
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def same_caller(a, b) -> bool:
    na, nb = normalise_phone(a), normalise_phone(b)
    # A short/garbage number must never match another short/garbage number.
    return bool(na) and len(na) == 10 and na == nb


def filter_facts(facts, allowlist) -> dict:
    """Only declared keys survive, each truncated (C4).

    An empty or missing allowlist yields NOTHING rather than everything. A misconfigured agent
    should be uninformed, never over-informed: the failure mode of the other default is a
    caller's balance and address in a transcript nobody meant to store.
    """
    if not isinstance(facts, dict) or not allowlist:
        return {}
    allowed = {str(k).strip().lower() for k in allowlist if str(k).strip()}
    out = {}
    for key, value in facts.items():
        k = str(key).strip().lower()
        if k not in allowed or value in (None, ""):
            continue
        out[k] = str(value)[:MAX_FACT_CHARS]
        if len(out) >= MAX_FACTS:
            break
    return out


def merge_identity(local, remote) -> tuple[str | None, str]:
    """The caller's display name, and where it came from (C9).

    OWEN wins on IDENTITY: `callers.label` is documented as a manual override, and the
    platform holds the line everywhere else that a human's entry beats a model's or a remote
    system's. The CRM wins on STATE, which is `facts`/`summary` and is not decided here.
    Authority is per field, not per system.
    """
    local_name = str((local or {}).get("display_name") or "").strip()
    remote_name = str((remote or {}).get("display_name") or "").strip()
    if local_name:
        return local_name, "owen"
    if remote_name:
        return remote_name, "provider"
    return None, "unknown"


def render_blob(local, remote, allowlist) -> tuple[str, list]:
    """The text prepended to the system prompt, and the field names that went into it.

    Returns (blob, injected_field_names). The NAMES are what gets logged — never the values
    (C4), so "why did the agent know that?" stays answerable without copying PII into
    `app_logs` as well as into the transcript.

    Returns ("", []) when there is nothing worth saying. An agent told "the caller is unknown"
    behaves worse than one simply not told anything: it tends to announce the fact.
    """
    local = local if isinstance(local, dict) else {}
    remote = remote if isinstance(remote, dict) else {}

    name, _source = merge_identity(local, remote)
    facts = filter_facts(remote.get("facts"), allowlist)
    summary = str(remote.get("summary") or "").strip()[:MAX_SUMMARY_CHARS]
    history = str(local.get("history") or "").strip()

    lines, injected = [], []
    if name:
        lines.append(f"The caller is {name}.")
        injected.append("display_name")
    if history:
        lines.append(history)
        injected.append("history")
    if summary:
        lines.append(summary)
        injected.append("summary")
    if facts:
        lines.append("Known details: " + "; ".join(f"{k}: {v}" for k, v in facts.items()))
        injected.extend(sorted(facts))

    if not lines:
        return "", []

    # The instruction matters as much as the data. Without it a model will recite what it was
    # given ("I see you have 3 open invoices and a balance of...") at a caller who only asked
    # about a leak, which reads as surveillance rather than service.
    lines.append(
        "Use this only if it is relevant to what the caller asks. Do not read it back to "
        "them, and do not assume it is complete or current."
    )
    return "Caller context:\n" + "\n".join(lines), injected


# --- provider configuration (C14/C16) ---------------------------------------------------

PROVIDER_KINDS = ("none", "ghl", "http")


def validate_provider(config) -> list:
    """Activation-time errors for an agent's `context_provider` block (C14).

    Checked when the version is activated rather than when a call arrives: an operator should
    learn their provider is misconfigured while editing, not from an agent that quietly
    greets nobody by name.
    """
    if config in (None, {}):
        return []
    if not isinstance(config, dict):
        return ["context_provider must be an object"]
    errors = []
    kind = str(config.get("kind") or "none").lower()
    if kind not in PROVIDER_KINDS:
        errors.append(
            f"context_provider.kind '{kind}' is unknown ({' | '.join(PROVIDER_KINDS)})"
        )
    if kind == "http":
        url = str(config.get("url") or "").strip()
        if not url:
            errors.append("context_provider.kind 'http' needs a url")
        elif not url.lower().startswith(("http://", "https://")):
            errors.append("context_provider.url must be http(s)")
    allow = config.get("allowlist")
    if allow is not None and not isinstance(allow, (list, tuple)):
        errors.append("context_provider.allowlist must be a list of field names")
    if kind in ("ghl", "http") and not allow:
        # Not an error: an agent may legitimately want only the name and the local history.
        # But silence here would be indistinguishable from a typo, so it is worth saying.
        errors.append(
            "context_provider has no allowlist, so no CRM facts can ever be injected — "
            "list the fields this agent may see, or set kind 'none'"
        )
    return errors
