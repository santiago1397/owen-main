"""Phase 4: a Spanish-speaking caller is answered in Spanish, automatically.

Pure tests -- no Deepgram, no OpenAI, no socket. The recogniser, the model and the voice are
all fakes; what is under test is the wiring between them, which is where this goes wrong
silently: a Spanish turn spoken by an English voice, or Spanish speech thrown away as noise,
raises nothing and still "works".

Pinned here:
  * a Spanish turn selects the Spanish voice, an English turn the English one;
  * an agent with no Spanish voice configured still works (runtime default Spanish voice);
  * Spanish speech is NOT dropped by the noise filter;
  * the detected language reaches the prompt, and the language rule is in it exactly once,
    for a persona agent and a bare one alike;
  * the language comes from the recogniser's report, and reaches the stored transcript.

Run:  python -m tests.test_spanish      (from owen-voice/)
"""

import asyncio
import sys
import types
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, ".")

# No I/O is exercised; stub the network libraries rather than require them.
for _m in ("httpx", "websockets"):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m)
        _mod.Timeout = lambda *a, **k: None
        _mod.AsyncClient = object
        sys.modules[_m] = _mod

from app.config import settings  # noqa: E402
from app.dsp import looks_like_latin_script  # noqa: E402
from app.providers import (DeepgramFluxSTT, TurnEvent, is_multilingual_stt,  # noqa: E402
                           primary_language)
from app.session import MediaSession  # noqa: E402

_checks = 0
_failures = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


class FakeTTS:
    """Records the voice each sentence was spoken in."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.voices: list = []

    def resolved_model(self, voice: str = "", model: str = "") -> str:
        return voice

    async def synthesize_stream(self, text, voice, instructions="", model=""):
        self.voices.append(voice)
        yield b"\x00\x00" * 160

    async def synthesize(self, text, voice, instructions="", model=""):
        self.voices.append(voice)
        return b"\x00\x00" * 160


class FakeLLM:
    """Replies with a fixed sentence and records the system prompt it was given."""

    def __init__(self, reply: str = "Claro, con gusto le ayudo.") -> None:
        self.reply_text = reply
        self.systems: list = []

    async def reply_stream(self, system, history, tools=None):
        self.systems.append(system)
        yield self.reply_text

    async def reply(self, system, history):
        self.systems.append(system)
        return self.reply_text


def build(agent: dict | None = None, tts: str = "deepgram", voice: str = "aura-2-delia-en",
          voice_es: str | None = None, reply: str = "Claro, con gusto le ayudo."):
    """A real Conversation with only the vendors replaced."""
    from app.pipeline import Conversation

    session = MediaSession(session_uuid="test")
    session.agent = dict(agent or {})
    session.tts_voice = voice
    session.tts_voice_es = voice_es
    convo = Conversation(session, writer=None)
    convo.tts = FakeTTS(tts)
    convo.llm = FakeLLM(reply)
    return convo


def run(coro):
    return asyncio.run(coro)


# --- voice selection ---------------------------------------------------------------------

def test_spanish_turn_selects_the_spanish_voice():
    print("\ntest_spanish_turn_selects_the_spanish_voice")
    convo = build(voice_es="aura-2-selena-es")
    run(convo._handle_turn(b"", text="Hola, tengo una gotera en el techo.", language="es"))
    check(convo.tts.voices == ["aura-2-selena-es"], "the agent's own Spanish voice speaks")
    check(convo.language == "es", "the conversation now knows the caller speaks Spanish")


def test_english_turn_selects_the_english_voice():
    print("\ntest_english_turn_selects_the_english_voice")
    convo = build(voice_es="aura-2-selena-es", reply="Sure, I can help with that.")
    run(convo._handle_turn(b"", text="Hi, I have a leak in my roof.", language="en"))
    check(convo.tts.voices == ["aura-2-delia-en"], "the English voice speaks an English turn")


def test_switching_language_mid_call_switches_voice():
    print("\ntest_switching_language_mid_call_switches_voice")
    convo = build(voice_es="aura-2-selena-es")
    run(convo._handle_turn(b"", text="Hi, I have a leak.", language="en"))
    run(convo._handle_turn(b"", text="Perdón, ¿habla español?", language="es"))
    run(convo._handle_turn(b"", text="Okay back to English please.", language="en"))
    check(convo.tts.voices == ["aura-2-delia-en", "aura-2-selena-es", "aura-2-delia-en"],
          "the voice follows the caller turn by turn")


def test_no_spanish_voice_configured_still_works():
    print("\ntest_no_spanish_voice_configured_still_works")
    convo = build(voice_es=None)
    run(convo._handle_turn(b"", text="Hola, necesito un presupuesto.", language="es"))
    check(convo.tts.voices == [settings.DG_TTS_VOICE_ES],
          f"Deepgram falls back to the default Spanish voice ({settings.DG_TTS_VOICE_ES})")
    check(settings.DG_TTS_VOICE_ES.startswith("aura-2-") and
          settings.DG_TTS_VOICE_ES.endswith("-es"),
          "and that default is a Spanish Aura-2 voice")
    check(convo.session.transcript[-1]["speaker"] == "agent", "the agent answered")


def test_english_voice_in_the_spanish_slot_is_not_used():
    print("\ntest_english_voice_in_the_spanish_slot_is_not_used")
    convo = build(voice_es="aura-2-thalia-en")
    run(convo._handle_turn(b"", text="Hola.", language="es"))
    check(convo.tts.voices == [settings.DG_TTS_VOICE_ES],
          "an English Aura-2 voice stored as voice_es falls back to the Spanish default")


def test_openai_voice_is_multilingual():
    print("\ntest_openai_voice_is_multilingual")
    convo = build(tts="openai", voice="alloy", voice_es=None)
    run(convo._handle_turn(b"", text="Hola, tengo una gotera.", language="es"))
    check(convo.tts.voices == ["alloy"],
          "OpenAI with no Spanish voice keeps the agent's own (multilingual) voice")
    convo = build(tts="openai", voice="alloy", voice_es="aura-2-celeste-es")
    run(convo._handle_turn(b"", text="Hola.", language="es"))
    check(convo.tts.voices == ["alloy"],
          "a Deepgram voice_es is never sent to OpenAI (that would be a 400 and silence)")


def test_greeting_is_english_before_anyone_speaks():
    print("\ntest_greeting_is_english_before_anyone_speaks")
    convo = build(voice_es="aura-2-selena-es")
    check(convo._voice_for(convo.language) == "aura-2-delia-en",
          "with no language detected yet the English voice is used (the greeting)")


# --- the noise filter ----------------------------------------------------------------------

def test_spanish_is_not_noise():
    print("\ntest_spanish_is_not_noise")
    for text in ("Sí.", "Sí, señor.", "Mañana.", "¿Cuándo pueden venir a revisar el techo?",
                 "Ñandú", "Está bien, gracias."):
        check(looks_like_latin_script(text), f"kept: {text!r}")
    for text in ("لا لا لا لا", "Привет", "你好你好"):
        check(not looks_like_latin_script(text), f"still dropped: {text!r}")
    convo = build()
    run(convo._handle_turn(b"", text="Sí.", language="es"))
    check(convo.session.noise_utterances == 0, "a one-word Spanish answer is not counted as noise")
    check(convo.session.turns == 1, "and it becomes a turn")


# --- the prompt ------------------------------------------------------------------------------

def test_language_rule_reaches_both_prompt_paths_once():
    print("\ntest_language_rule_reaches_both_prompt_paths_once")
    rule = settings.AGENT_LANGUAGE_RULE
    check("ALWAYS reply in English" not in settings.AGENT_SYSTEM_PROMPT,
          "the English-only instruction is gone from the default prompt")
    bare = build()
    check(bare.system_prompt.count(rule) == 1, "a bare agent gets the language rule once")
    persona = build(agent={"persona": "You are Dream Team Roofing's receptionist."})
    p = persona.system_prompt
    check(p.count(rule) == 1, "a persona agent gets the same rule, once")
    check("Dream Team Roofing's receptionist" in p, "and keeps its persona")
    check(settings.AGENT_SYSTEM_PROMPT not in p, "the default prompt does not leak in")


def test_detected_language_reaches_the_model():
    print("\ntest_detected_language_reaches_the_model")
    convo = build()
    run(convo._handle_turn(b"", text="Hola, tengo una gotera.", language="es"))
    check("speaking Spanish" in convo.llm.systems[-1],
          "the turn's prompt tells the model the caller is speaking Spanish")
    run(convo._handle_turn(b"", text="Sí.", language=""))
    check(convo.language == "es" and "speaking Spanish" in convo.llm.systems[-1],
          "a turn with no report keeps the language rather than snapping back to English")


# --- the recogniser's report -----------------------------------------------------------------

def test_language_comes_from_the_recogniser():
    print("\ntest_language_comes_from_the_recogniser")
    check(primary_language(["es-419", "en"]) == "es", "primary entry, base code")
    check(primary_language(["en"]) == "en", "English")
    check(primary_language([]) == "" and primary_language(None) == "",
          "no report is no language, not a guess")
    check(TurnEvent("end", "hola").language == "", "TurnEvent defaults to no language")


def test_flux_url_hints_only_on_the_multilingual_model():
    print("\ntest_flux_url_hints_only_on_the_multilingual_model")
    saved = (settings.DG_STT_MODEL, settings.DG_STT_LANGUAGE_HINTS)
    try:
        settings.DG_STT_MODEL, settings.DG_STT_LANGUAGE_HINTS = "flux-general-multi", "en,es"
        q = parse_qs(urlparse(DeepgramFluxSTT()._url()).query)
        check(q.get("model") == ["flux-general-multi"], "the multilingual model is requested")
        check(q.get("language_hint") == ["en", "es"], "English and Spanish are hinted")
        check(q.get("sample_rate") == ["8000"], "still 8 kHz linear16, no resampling")
        settings.DG_STT_MODEL = "flux-general-en"
        q = parse_qs(urlparse(DeepgramFluxSTT()._url()).query)
        check("language_hint" not in q,
              "flux-general-en gets no hint (Deepgram answers that with a 400)")
        check(is_multilingual_stt("flux-general-multi") and not is_multilingual_stt(
            "flux-general-en"), "the model test")
    finally:
        settings.DG_STT_MODEL, settings.DG_STT_LANGUAGE_HINTS = saved


def test_flux_turninfo_languages_are_parsed():
    print("\ntest_flux_turninfo_languages_are_parsed")
    import json

    class FakeWS:
        def __init__(self, msgs):
            self.msgs = msgs

        def __aiter__(self):
            async def gen():
                for m in self.msgs:
                    yield json.dumps(m)
            return gen()

    stt = DeepgramFluxSTT()
    stt._ws = FakeWS([
        {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "Hola, buenas tardes",
         "end_of_turn_confidence": 0.9, "languages": ["es"], "languages_hinted": ["en", "es"]},
        {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "Hello",
         "end_of_turn_confidence": 0.9},
    ])
    run(stt._read_loop())
    a, b = stt.events.get_nowait(), stt.events.get_nowait()
    check(a.language == "es" and a.transcript == "Hola, buenas tardes",
          "a Spanish TurnInfo becomes a Spanish TurnEvent")
    check(b.language == "", "a TurnInfo with no `languages` (flux-general-en) carries none")


def test_pump_carries_the_language_into_the_turn():
    print("\ntest_pump_carries_the_language_into_the_turn")
    convo = build()
    seen: list = []

    def _begin(text, drafted=False, language=""):
        seen.append((text, drafted, language))

        async def _noop():
            await asyncio.sleep(3600)
        convo._turn = asyncio.get_event_loop().create_task(_noop())
        return convo._turn

    class Q:
        def __init__(self):
            self.events = asyncio.Queue()

    async def go():
        convo.stream_stt = Q()
        convo._begin_turn = _begin
        pump = asyncio.create_task(convo._pump_turn_events())
        convo.stream_stt.events.put_nowait(TurnEvent("eager_end", "hola", language="es"))
        convo.stream_stt.events.put_nowait(TurnEvent("resumed"))
        convo.stream_stt.events.put_nowait(TurnEvent("end", "hola buenas", language="es"))
        for _ in range(8):
            await asyncio.sleep(0)
        pump.cancel()
        if convo._turn is not None:
            convo._turn.cancel()

    run(go())
    check(seen == [("hola", True, "es"), ("hola buenas", False, "es")],
          "both the eager draft and the committed turn carry the detected language")


# --- what OWEN stores ------------------------------------------------------------------------

def test_transcript_records_the_language():
    print("\ntest_transcript_records_the_language")
    convo = build()
    run(convo._handle_turn(b"", text="Hola.", language="es"))
    run(convo._handle_turn(b"", text="Necesito un presupuesto.", language="es"))
    run(convo._handle_turn(b"", text="OK.", language="en"))
    t = convo.session.transcript
    check(t[0] == {"speaker": "caller", "text": "Hola.", "language": "es"},
          "caller segments carry the recogniser's language")
    check(t[1].get("language") == "es", "agent segments carry the language they were spoken in")
    check(convo.session.language == "es", "the call's language is its majority language")
    check(convo.session.turn_metrics[0]["language"] == "es",
          "per-turn metrics carry it, so latency can be split by language")
    plain = build()
    run(plain._handle_turn(b"", text="Hello.", language=""))
    check(plain.session.transcript[0] == {"speaker": "caller", "text": "Hello."},
          "with no report the segment keeps its old two-key shape")
    check(plain.session.language == "", "and the call's language is unknown, not 'en'")


def test_default_filler_is_spanish_on_a_spanish_call():
    print("\ntest_default_filler_is_spanish_on_a_spanish_call")
    from app.custom_tools import DEFAULT_FILLER, DEFAULT_FILLER_ES

    spoken: list = []
    convo = build()
    convo.language = "es"
    convo.custom = [{"name": "lookup", "mode": "sync", "filler": DEFAULT_FILLER,
                     "method": "GET", "url": "http://example.invalid/"}]
    convo._pending_custom = [("lookup", {})]

    async def _speak(text):
        spoken.append(text)
        return 1
    convo._speak = _speak

    import app.providers as providers

    async def _no_call(tool, args):
        return 200, {}
    saved = providers.call_custom_tool
    providers.call_custom_tool = _no_call
    try:
        run(convo._run_custom_tools())
    finally:
        providers.call_custom_tool = saved
    check(spoken == [DEFAULT_FILLER_ES], "the default filler is spoken in Spanish")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_checks} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1 if _failures else 0)
