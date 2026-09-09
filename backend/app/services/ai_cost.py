"""Pure AI-cost kernel (AI_AGENT_SPEC D14).

AI COST IS DERIVED, NOT RATED — and the distinction is load-bearing.

`services/billing.py` opens by insisting usage cost is never estimated, because BulkVS
publishes its own RATED call detail records and an earlier design that estimated from
Asterisk's CDR was found wrong in both directions. AI vendors issue no per-call charge at
all: the only honest thing available is usage they report (tokens, audio seconds, characters)
multiplied by a published rate. That is much better than a guess and still not an invoice, so
every row this module produces is stamped `derived` and shown separately from carrier
`rated` rows.

Where usage is missing, the row is marked **unrated** rather than counted as zero. Same rule
the carrier kernel already holds itself to: "a bill that quietly under-reports is worse than
one that admits ignorance."

Rates are per UNIT and configurable, because they change often and are not ours to control.
The shipped defaults reflect published list prices at the time of writing and should be
checked against a real invoice before anyone plans around them.

Stdlib only, so it is testable in a bare sandbox like the billing kernel it sits beside.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Charge kinds written to call_charges.
KIND_AI_STT = "ai.stt"
KIND_AI_LLM = "ai.llm"
KIND_AI_TTS = "ai.tts"
AI_KINDS = (KIND_AI_STT, KIND_AI_LLM, KIND_AI_TTS)

PROVENANCE_RATED = "rated"      # the carrier issued this figure
PROVENANCE_DERIVED = "derived"  # we computed it from vendor-reported usage

# Published list prices, per unit, USD. Deliberately explicit rather than folded into a
# per-minute figure: a talkative caller and a quiet one cost very different amounts for the
# same wall-clock minute, and a per-minute average hides exactly the thing worth watching.
DEFAULT_RATES = {
    # --- STT, per second ---
    # BATCH vendors bill only the audio actually sent (one utterance per turn).
    "stt.gpt-4o-mini-transcribe": Decimal("0.0001"),
    "stt.whisper-1": Decimal("0.0001"),
    # STREAMING vendors bill the SOCKET, not the speech: an hour-long connection carrying
    # thirty minutes of audio is billed for the hour. owen-voice reports wall-clock seconds
    # for these (pipeline stamps usage["stt_billing"]="stream"), so the same per-second unit
    # is correct here -- but the seconds mean something different, and the 5x cost jump this
    # produces is real and expected (VOICE_STACK_MIGRATION M9), not a bug to be tuned away.
    "stt.flux-general-en": Decimal("0.00012833"),      # Deepgram $0.0077/min PAYG / 60
    "stt.flux-general-multi": Decimal("0.00012833"),
    "stt.nova-3": Decimal("0.00012833"),
    # --- LLM, per 1000 tokens, split in/out ---
    "llm.in.gpt-4o-mini": Decimal("0.00015"),
    "llm.out.gpt-4o-mini": Decimal("0.0006"),
    # --- TTS, per 1000 characters ---
    "tts.tts-1": Decimal("0.015"),
    "tts.aura-2": Decimal("0.030"),                    # Deepgram PAYG
    # `gpt-4o-mini-tts` is DELIBERATELY ABSENT. It bills in AUDIO TOKENS, and OpenAI publishes
    # no character-to-token conversion, so there is no honest per-character rate to put here.
    # It previously carried 0.015 -- the `tts-1` per-CHARACTER rate applied to a model that is
    # not billed per character, which is confidently wrong rather than unknown. Omitting the
    # key makes tts_charge() emit an `unrated` row with reason "no rate for model", which is
    # this module's stated contract: a bill that quietly under-reports is worse than one that
    # admits ignorance.
}

# Aura-2 ships ~49 English voices as separate model ids (aura-2-thalia-en, aura-2-apollo-en,
# ...) that all bill identically. Rating each by name would mean a table edit every time an
# operator picks a different voice -- and a silent `unrated` row when someone forgets.
_RATE_ALIASES = (("stt.flux-", "stt.flux-general-en"), ("tts.aura-2-", "tts.aura-2"))

_CENTS = Decimal("0.000001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass
class AiCharge:
    """One derived cost line for one call."""

    kind: str
    rate_code: str
    amount: Decimal
    usage: dict
    unrated: bool = False
    unrated_reason: str | None = None


def _rate(rates: dict, code: str):
    table = rates or DEFAULT_RATES
    value = table.get(code)
    if value is None:
        # Fall back to a family rate before giving up: a voice variant is not a new price.
        # An explicit entry always wins, so overriding one voice stays possible.
        for prefix, family in _RATE_ALIASES:
            if code.startswith(prefix) and family in table:
                value = table[family]
                break
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a malformed rate is a missing rate
        return None


def stt_charge(model: str, audio_seconds, rates: dict | None = None) -> AiCharge:
    code = f"stt.{model or 'unknown'}"
    rate = _rate(rates, code)
    if rate is None or audio_seconds in (None, ""):
        return AiCharge(KIND_AI_STT, code, Decimal("0"), {"audio_seconds": audio_seconds},
                        unrated=True,
                        unrated_reason="no rate for model" if rate is None else "no usage")
    amount = _q(rate * Decimal(str(audio_seconds)))
    return AiCharge(KIND_AI_STT, code, amount, {"audio_seconds": float(audio_seconds)})


def llm_charge(model: str, tokens_in, tokens_out, rates: dict | None = None) -> AiCharge:
    code = f"llm.{model or 'unknown'}"
    r_in = _rate(rates, f"llm.in.{model}")
    r_out = _rate(rates, f"llm.out.{model}")
    usage = {"tokens_in": tokens_in, "tokens_out": tokens_out}
    if r_in is None or r_out is None or tokens_in is None or tokens_out is None:
        return AiCharge(KIND_AI_LLM, code, Decimal("0"), usage, unrated=True,
                        unrated_reason="no rate for model" if r_in is None or r_out is None
                        else "no usage reported")
    amount = _q(
        (Decimal(str(tokens_in)) / 1000) * r_in + (Decimal(str(tokens_out)) / 1000) * r_out
    )
    return AiCharge(KIND_AI_LLM, code, amount, usage)


def tts_charge(model: str, characters, rates: dict | None = None) -> AiCharge:
    code = f"tts.{model or 'unknown'}"
    rate = _rate(rates, code)
    if rate is None or characters in (None, ""):
        return AiCharge(KIND_AI_TTS, code, Decimal("0"), {"characters": characters},
                        unrated=True,
                        unrated_reason="no rate for model" if rate is None else "no usage")
    amount = _q((Decimal(str(characters)) / 1000) * rate)
    return AiCharge(KIND_AI_TTS, code, amount, {"characters": int(characters)})


def charges_for_session(usage: dict, rates: dict | None = None) -> list[AiCharge]:
    """Every derived line for one agent session, from the usage the runtime reported.

    An absent stage produces an UNRATED row rather than nothing: a call whose TTS usage went
    unreported should read as "we do not know what this cost", never as free."""
    u = usage or {}
    return [
        stt_charge(u.get("stt_model"), u.get("stt_audio_seconds"), rates),
        llm_charge(u.get("llm_model"), u.get("llm_tokens_in"), u.get("llm_tokens_out"), rates),
        tts_charge(u.get("tts_model"), u.get("tts_characters"), rates),
    ]


def session_total(charges: list[AiCharge]) -> tuple[Decimal, bool]:
    """(total, any_unrated). The flag matters as much as the number — a total that silently
    omits an unpriced stage is a smaller lie than a wrong one only by accident."""
    total = sum((c.amount for c in charges), Decimal("0"))
    return _q(total), any(c.unrated for c in charges)
