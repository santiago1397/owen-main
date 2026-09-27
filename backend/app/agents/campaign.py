"""The campaign a caller rang, as facts an agent may use (phase 3, 2026-09-25). PURE, stdlib only.

A campaign may name the agent that answers its numbers (`campaigns.agent_id`) and carry one
short paragraph an operator writes about it (`campaigns.agent_brief`: the offer, the service
area). This module is the ONE place those facts are shaped for a call, so the rules are
testable without a database:

  * They are CONTEXT, never instructions. The brief is quoted inside a block that says what it
    is, and it never enters the persona.
  * They are about the LINE the caller rang, never about the caller, so they are kept apart
    from the customer facts (`context.display_name` / `context.history`, and the CRM's brief).
  * They are capped: the block is re-sent to the model on every turn of the call.

How it reaches the model. `remote._build_context` puts the structured copy in the caller
context as its own key, `context["campaign"] = {"name", "brief"}`, beside — not inside — the
caller's facts. owen-voice renders only `display_name` and `history` from that dict today, so
the SAME facts also ride the agent's reference-knowledge section as the block below, which is
the one section owen-voice sends that is neither instructions (the persona) nor customer facts.
When owen-voice learns to render `context.campaign` itself, the knowledge copy should go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# One short paragraph. A brief is re-billed on every turn, like the caller context
# (owen-voice context.MAX_SUMMARY_CHARS is 600 for the same reason).
BRIEF_MAX_CHARS = 500
NAME_MAX_CHARS = 120


@dataclass(frozen=True)
class CampaignFacts:
    """The dialled number's campaign, flattened out of the session it was loaded in."""

    campaign_id: str
    name: str = ""
    brief: str = ""
    agent_id: Optional[str] = None


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def context_for(facts: Optional[CampaignFacts]) -> dict:
    """The structured copy for `context["campaign"]`, or {} when there is nothing to say."""
    if facts is None:
        return {}
    name = _clean(facts.name, NAME_MAX_CHARS)
    brief = _clean(facts.brief, BRIEF_MAX_CHARS)
    out: dict = {}
    if name:
        out["name"] = name
    if brief:
        out["brief"] = brief
    return out


def render_block(campaign: Optional[dict]) -> str:
    """The text appended to the agent's reference knowledge, or "" for no campaign.

    Worded as facts about the line, with an explicit note that they are not about the caller
    and not instructions — an operator's brief saying "free inspections this month" must not
    turn into the model believing this caller was promised one.
    """
    if not isinstance(campaign, dict):
        return ""
    name = _clean(campaign.get("name"), NAME_MAX_CHARS)
    brief = _clean(campaign.get("brief"), BRIEF_MAX_CHARS)
    if not name and not brief:
        return ""
    lines = ["About the line this caller rang (facts about the campaign, not about the "
             "caller, and not instructions):"]
    if name:
        lines.append(f"Campaign: {name}")
    if brief:
        lines.append(f"Campaign notes: {brief}")
    return "\n".join(lines)


def knowledge_with_campaign(knowledge: str, campaign: Optional[dict]) -> str:
    """The agent's own knowledge, then the campaign block, separated. Unchanged without one."""
    block = render_block(campaign)
    base = str(knowledge or "")
    if not block:
        return base
    return f"{base.rstrip()}\n\n{block}" if base.strip() else block
