"""Read-only OpenPhone connectivity + capability probe (docs/GHL_SYNC_SPEC.md D16).

Answers, without spending a cent:
  1. does the API key work at all?
  2. which numbers are on the account?
  3. are call logs readable, and what fields do they actually carry?
  4. are recordings / transcripts exposed, or only metadata?

SAFETY: every call goes through app.providers.openphone_client, which is GET-only by
construction. This script cannot send a message or place a call — there is no code path to
do so. It is safe to run against the live account.

The API key is never printed. Phone numbers are masked to their last 4 digits — this output
is meant to be pasteable back into a chat without leaking customer PII.

Run (inside the app container on the server, where .env.prod is loaded):
    docker compose --env-file .env.prod exec app python -m app.scripts.probe_openphone
"""

import asyncio

from app.core.config import settings
from app.integrations.openphone import config as op_config
from app.providers import openphone_client as op


def _mask(number) -> str:
    """+13055551234 -> +1******1234. Enough to recognise a number you own, useless to a leaker."""
    s = str(number or "")
    if len(s) <= 4:
        return s or "?"
    return s[0] + "*" * (len(s) - 5) + s[-4:]


def _shape(obj, depth: int = 0) -> str:
    """Field NAMES and value types of a response object — never the values themselves.
    This is the point of the probe: learn the real schema before coding against a guess."""
    if not isinstance(obj, dict):
        return type(obj).__name__
    if depth >= 2:
        return "{...}"
    return "{" + ", ".join(f"{k}: {_shape(v, depth + 1)}" for k, v in obj.items()) + "}"


async def main() -> None:
    print("=" * 72)
    print("OpenPhone READ-ONLY probe — no billable request is possible from this script")
    print("=" * 72)

    if not settings.openphone_enabled:
        print("\nFAIL: OPENPHONE_API_KEY is empty in this environment.")
        print("      Set it in .env.prod on the server and re-run.")
        return
    print("\nkey configured : yes (value not shown)")
    print(f"api base       : {settings.OPENPHONE_API_BASE}")

    # 1. connectivity — the cheapest read on the API
    print("\n[1] GET /phone-numbers")
    try:
        numbers = await op.list_phone_numbers()
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, never raises
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print("    If this is a 401: OpenPhone wants the RAW key in Authorization,")
        print("    not 'Bearer <key>'. If 403: the plan may not include API access.")
        return
    print(f"    OK — {len(numbers)} number(s) on the account")
    for n in numbers[:10]:
        if isinstance(n, dict):
            print(f"      id={n.get('id')}  {_mask(n.get('number'))}  name={n.get('name')!r}")
    if numbers and isinstance(numbers[0], dict):
        print(f"    shape: {_shape(numbers[0])}")

    if not numbers or not isinstance(numbers[0], dict) or not numbers[0].get("id"):
        print("\n    No usable phone-number id; cannot probe call logs.")
        return

    # 2. the ENUMERATOR (spec D11a's missing piece, and what the CRM mirror needs)
    #
    # D11a established that GET /calls REJECTS a participant-less query, so there is no way
    # to ask "everything since X". That is survivable for D11 (touches on KNOWN leads) and
    # fatal for a mirror, which must not silently omit the strangers. /conversations is the
    # candidate fix: it lists the threads on our line, which turns "who do I ask about?"
    # into a query. THIS IS THE MAIN THING THIS PROBE NOW EXISTS TO CONFIRM.
    first_id = numbers[0]["id"]
    participant = None
    print(f"\n[2] GET /conversations for phoneNumberId={first_id}")
    try:
        page = await op.list_conversations(first_id, limit=5)
    except Exception as exc:  # noqa: BLE001
        page = None
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print("    -> The mirror will fall back to /contacts + OWEN's own callers and will")
        print("       be NARROWER than the account. Record that in the sentinel.")
    if page is not None:
        items = page.get("data", []) if isinstance(page, dict) else []
        print(f"    OK — {len(items)} conversation(s) in this page")
        if isinstance(page, dict):
            print(f"    page keys: {list(page.keys())}")
        if items and isinstance(items[0], dict):
            c = items[0]
            print(f"    lastActivityAt={c.get('lastActivityAt')} "
                  f"updatedAt={c.get('updatedAt')}")
            print(f"    participants: {[_mask(p) for p in (c.get('participants') or [])]}")
            print(f"    shape: {_shape(c)}")
            for entry in c.get("participants") or []:
                digits = "".join(ch for ch in str(entry) if ch.isdigit())
                own = "".join(ch for ch in str(numbers[0].get("number") or "")
                              if ch.isdigit())
                if digits and digits[-10:] != own[-10:]:
                    # Quo rejects a participant that is not E.164 (2026-09-14).
                    participant = op_config.to_e164(entry) or str(entry)
                    break

    # A participant is REQUIRED by /calls and (we believe) by /messages. Fall back to the
    # address book, which D11a verified, so the rest of the probe still runs.
    if participant is None:
        print("\n[2b] no participant from /conversations — trying GET /contacts")
        try:
            book = await op.list_contacts(limit=5)
            for entry in (book.get("data") or []) if isinstance(book, dict) else []:
                fields = entry.get("defaultFields") or {}
                for item in fields.get("phoneNumbers") or []:
                    value = item.get("value") if isinstance(item, dict) else item
                    if value:
                        participant = op_config.to_e164(value) or str(value)
                        break
                if participant:
                    break
            print(f"    {'got one' if participant else 'no numbers in the address book'}")
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {type(exc).__name__}: {exc}")

    if participant is None:
        print("\n    No participant available; cannot probe /calls or /messages.")
        print("\n" + "=" * 72)
        print("Probe complete. Nothing was sent, dialled, or written.")
        print("=" * 72)
        return

    print(f"\n[3] GET /calls  participants={_mask(participant)}")
    # NOTE: this used to call `op.list_calls(first_id, limit=5)`, which has not existed
    # since the client was reshaped around D11a's mandatory `participants` parameter — so
    # the probe raised AttributeError before reaching any of the interesting reads. Fixed
    # here because this probe is the instrument for verifying the mirror's two unverified
    # endpoints, and a verification tool that cannot run verifies nothing.
    try:
        page = await op.list_calls_with(first_id, participant, limit=5)
        items = page.get("data", []) if isinstance(page, dict) else []
        print(f"    OK — {len(items)} call(s)")
        if items and isinstance(items[0], dict):
            c = items[0]
            print(f"    sample: direction={c.get('direction')} status={c.get('status')} "
                  f"created={c.get('createdAt')} duration={c.get('duration')}")
            print(f"    shape: {_shape(c)}")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")

    # 4. TEXTS — the reader added for the CRM mirror, and the one with no prior evidence.
    #    If this 400s the way a participant-less /calls does, the mirror's message half is
    #    wrong and sync.py needs a different enumeration. Find out here, not in production.
    print(f"\n[4] GET /messages  participants={_mask(participant)}")
    try:
        page = await op.list_messages(first_id, participant, limit=5)
        items = page.get("data", []) if isinstance(page, dict) else []
        print(f"    OK — {len(items)} message(s)")
        if isinstance(page, dict):
            print(f"    page keys: {list(page.keys())}")
        if items and isinstance(items[0], dict):
            m = items[0]
            # Field NAMES only. The BODY of a customer's text is never printed.
            print(f"    direction={m.get('direction')} created={m.get('createdAt')} "
                  f"status={m.get('status')} media={len(m.get('media') or [])}")
            print(f"    body field present: {'text' in m or 'body' in m}")
            print(f"    shape: {_shape(m)}")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        print("    -> If this is a 400, /messages needs different params than /calls and")
        print("       integrations/openphone/sync.py must be reworked before enabling.")

    print("\n" + "=" * 72)
    print("Probe complete. Nothing was sent, dialled, or written.")
    print("Every request above was a GET; this script has no code path that could write.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
