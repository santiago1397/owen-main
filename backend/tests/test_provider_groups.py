"""Unit test for the Calls/Messages provider buckets (app/api/provider_groups.py).

Regression: "attribution" was hardcoded to ("twilio", "signalwire"), so every call on the
second Twilio account ("twilio-b", ~1k calls) was invisible on BOTH Calls tabs.

Run: python -m tests.test_provider_groups
"""

import json

from app.api.provider_groups import provider_group_names
from app.core.config import settings


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"provider_groups failed at: {name}")


def test_every_twilio_account_is_attribution():
    saved = settings.TWILIO_ACCOUNTS
    try:
        settings.TWILIO_ACCOUNTS = json.dumps([
            {"name": "twilio", "sid": "AC1", "token": "t1"},
            {"name": "twilio-b", "sid": "AC2", "token": "t2"},
        ])
        names = provider_group_names("attribution")
        check("twilio-b is in attribution", "twilio-b" in names)
        check("twilio is in attribution", "twilio" in names)
        check("signalwire is in attribution", "signalwire" in names)
        check("no duplicates", len(names) == len(set(names)))
        check("platform providers are not attribution", not {"bulkvs", "asterisk"} & set(names))

        settings.TWILIO_ACCOUNTS = ""
        check("twilio kept even without credentials", "twilio" in provider_group_names("attribution"))
    finally:
        settings.TWILIO_ACCOUNTS = saved


def test_platform_and_unknown():
    check("platform = bulkvs + asterisk", set(provider_group_names("platform")) == {"bulkvs", "asterisk"})
    check("unknown group = no filter", provider_group_names("nope") is None)
    check("absent group = no filter", provider_group_names(None) is None)


if __name__ == "__main__":
    test_every_twilio_account_is_attribution()
    test_platform_and_unknown()
    print("provider_groups: all passed")
