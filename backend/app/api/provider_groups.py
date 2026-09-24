"""Provider buckets shared by the Calls page and the Messages inbox (Ticket 06).

"attribution" is the legacy tracking stack: SignalWire plus EVERY configured Twilio account.
Twilio accounts are named in TWILIO_ACCOUNTS (e.g. "twilio", "twilio-b"), so the bucket is
derived from settings rather than hardcoded — a hardcoded ("twilio", "signalwire") silently
hid every "twilio-b" call from both tabs.
"""

from app.core.config import settings

PLATFORM_PROVIDERS: tuple[str, ...] = ("bulkvs", "asterisk")


def provider_group_names(group: str | None) -> tuple[str, ...] | None:
    """Provider names in `group`, or None for an unknown/absent group (= no filter)."""
    if group == "attribution":
        # "twilio" stays even if its credentials are blank: historic rows still carry it.
        twilio = dict.fromkeys(["twilio", *(a.name for a in settings.twilio_accounts())])
        return ("signalwire", *twilio)
    if group == "platform":
        return PLATFORM_PROVIDERS
    return None
