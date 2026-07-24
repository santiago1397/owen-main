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
import json

from app.core.config import settings
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
    print(f"\nkey configured : yes (value not shown)")
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

    # 2. call logs — the data the whole OpenPhone integration depends on (spec D11)
    first_id = numbers[0]["id"]
    print(f"\n[2] GET /calls for phoneNumberId={first_id}")
    try:
        page = await op.list_calls(first_id, limit=5)
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return
    items = page.get("data", []) if isinstance(page, dict) else []
    print(f"    OK — {len(items)} call(s) in this page")
    if isinstance(page, dict):
        print(f"    page keys: {list(page.keys())}")
    if items and isinstance(items[0], dict):
        c = items[0]
        print(f"    sample: direction={c.get('direction')} status={c.get('status')} "
              f"created={c.get('createdAt')} duration={c.get('duration')}")
        print(f"    participants: {[_mask(p) for p in (c.get('participants') or [])]}")
        print(f"    shape: {_shape(c)}")

        # 3. is there richer content than metadata? (affects spec open item 3)
        print("\n[3] capability check on that call")
        for label, key in (("recording", "recordingUrl"), ("transcript", "transcriptId")):
            present = key in c or any(key.lower() in k.lower() for k in c)
            print(f"    {label:11s}: {'field present' if present else 'not in call payload'}")
    else:
        print("    No calls returned — the account may have no recent activity on this number.")

    print("\n" + "=" * 72)
    print("Probe complete. Nothing was sent, dialled, or written.")
    print("Paste this output back to decide the Phase 3 ingestion shape.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
