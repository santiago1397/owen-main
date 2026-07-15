"""Twilio webhook surface — PUBLIC, signature-verified, never JWT (ARCHITECTURE.md #11)."""

from app.providers.twilio import TwilioAdapter
from app.webhooks.common import build_router

router = build_router(TwilioAdapter(), "twilio", ["X-Twilio-Signature"])
