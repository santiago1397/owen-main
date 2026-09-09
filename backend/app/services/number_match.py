"""The ONE rule for "which `numbers` row is this DID?" — pure, no I/O.

WHY THIS FILE EXISTS. The rule was implemented twice, and the two copies drifted:

  2026-07-17  ingest_message_event matches `numbers` on (provider_id, phone_number).
              Correct at the time: SignalWire was the only SMS provider and DIDs had a
              single identity.
  2026-07-23  the CALL path hits the BulkVS split-identity case and is fixed —
              "Attribute inbound BulkVS calls to their Number instead of dropping it".
              The SMS sibling is not revisited.
  2026-09-09  found: 3 of 6 messages had no number and no campaign, for seven weeks,
              because nobody knew a second copy of the rule existed 60 lines away.

So the rule lives here once and both ingest paths call it. A future provider changes one
function, not two — and the test suite asserts both paths agree, so a third copy cannot
quietly appear.

THE RULE. A DID has a SPLIT IDENTITY (see the Number model): `owner_provider` is the carrier
it is registered with ("bulkvs") and `media_provider` is who carries its traffic
("asterisk"). `provider_id` is the row's ORIGINAL provider and is deliberately left alone
when number_sync adopts a legacy DID, so the row's history stays intact — which means it can
disagree with both of the others.

The three ingest paths each arrive under a different provider name:

    inbound CALL  -> 'asterisk' (the ARI consumer)   matches media_provider
    inbound SMS   -> 'bulkvs'   (the MO webhook)     matches owner_provider
    legacy T/SW   -> 'twilio' / 'signalwire'         matches provider_id (owner/media NULL)

so all three have to be in the match. It is a widening, never a narrowing: every row a
provider_id-only lookup found is still found.
"""

from __future__ import annotations

from sqlalchemy import or_

from app.models import Number


def owned_number_clause(provider_id: int, provider_name: str):
    """SQLAlchemy filter selecting the `numbers` row that owns a DID for this provider.

    Pair it with `Number.phone_number == <did>`; on its own it is only the provider half.
    """
    return or_(
        Number.provider_id == provider_id,
        Number.owner_provider == provider_name,
        Number.media_provider == provider_name,
    )
