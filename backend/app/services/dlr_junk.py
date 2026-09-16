"""The marker that hides a stored delivery receipt from every thread (2026-09-16).

Its own tiny module, stdlib-only, because three places need to agree on one string and none
of them should import the others: the webhook that stores an orphan receipt, the Inbox query
that must not show one, and the backfill command that marks the ones already in the table.

## Why a marker rather than a column, or a delete

A **column** would mean an Alembic migration against a live telephony database for a flag on
a handful of junk rows. The owner's standing rule is additive-only, and `raw_payload` is
JSONB that is already written by several paths — this ADDS a key beside them and overrides
nothing.

A **delete** would be simpler and is refused on purpose. These rows are the only record that
a carrier ever said anything about a text, and the sweep that removes them is the same code
that would remove a customer's message if the detection were ever wrong. Hiding is
reversible; deleting is not, and the rule this whole change exists to honour is that a real
customer text is never lost.
"""

DLR_JUNK_KEY = "bulkvs_dlr_junk"


def is_junk(raw_payload) -> bool:
    """True when this `messages` row is a stored delivery receipt, not a message.

    Defensive about the shape: `raw_payload` is JSONB written by several different paths,
    and a row with something unexpected in it must answer "not junk" — the safe direction,
    because the cost of a wrong False is one visible junk row and the cost of a wrong True
    is a hidden customer message.
    """
    return isinstance(raw_payload, dict) and isinstance(raw_payload.get(DLR_JUNK_KEY), dict)
