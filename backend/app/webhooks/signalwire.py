"""SignalWire webhook surface — PUBLIC, signature-verified (ARCHITECTURE.md #11, #12).

Accepts either header the SignalWire space may send; the SignalWireAdapter keys the
HMAC with the SignalWire token (not Twilio's).
"""

from app.providers.signalwire import SignalWireAdapter
from app.webhooks.common import build_router

router = build_router(
    SignalWireAdapter(), "signalwire", ["X-SignalWire-Signature", "X-Twilio-Signature"]
)
