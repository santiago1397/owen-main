"""Unit test for the TTS cache-keying + prompt-extraction kernels (app.services.tts, 15.2).

Dependency-free: only the PURE helpers are exercised (cache_key / is_media_uri /
graph_prompt_texts import nothing beyond stdlib — settings/httpx are lazy inside the I/O
functions, which need a network + ffmpeg and are exercised on the server, not here).

Asserts:
- cache_key is sha256 of "text|voice" — deterministic, text- and voice-sensitive
  (the content address that makes activation prewarm + call-time lookup agree);
- is_media_uri recognizes ARI media schemes (those strings must NEVER be TTS'd);
- graph_prompt_texts collects each node's EFFECTIVE prompt (first present key, matching
  what the interpreter plays), de-duplicates, and skips media URIs / {{...}} templates /
  non-prompt node types.

Run: python -m tests.test_tts_prompts
"""

import hashlib
import sys

from app.services.tts import cache_key, graph_prompt_texts, is_media_uri


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"tts_prompts failed at: {name}")


def test_cache_key():
    print("cache_key — sha256(text|voice), deterministic + input-sensitive:")
    key = cache_key("Thanks for calling.", "alloy")
    check("key is sha256 of 'text|voice'",
          key == hashlib.sha256(b"Thanks for calling.|alloy").hexdigest())
    check("deterministic", cache_key("Thanks for calling.", "alloy") == key)
    check("different text -> different key", cache_key("Goodbye.", "alloy") != key)
    check("different voice -> different key", cache_key("Thanks for calling.", "nova") != key)
    check("hex filename-safe (64 chars)", len(key) == 64 and all(c in "0123456789abcdef" for c in key))


def test_is_media_uri():
    print("is_media_uri — ARI media schemes pass through, text does not:")
    check("sound: is a media URI", is_media_uri("sound:owen/greeting") is True)
    check("recording: is a media URI", is_media_uri("recording:1690.1-vm-1") is True)
    check("plain text is not", is_media_uri("Thanks for calling Dream Team!") is False)
    check("bare sound name is not (goes through resolution)", is_media_uri("welcome") is False)


def test_graph_prompt_texts():
    print("graph_prompt_texts — effective prompts only, deduped; URIs/templates skipped:")
    graph = {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "greet"}},
            "greet": {"type": "play", "prompt": "Thanks for calling Dream Team!",
                      "next": {"default": "menu"}},
            "menu": {"type": "menu", "media": "Press 1 for sales, 2 for support.",
                     "next": {"1": "sales"}},
            # media (a URI) takes priority over prompt — the interpreter plays media, so
            # the shadowed prompt text must NOT be synthesized.
            "shadowed": {"type": "play", "media": "sound:owen/provisioned",
                         "prompt": "never played", "next": {"default": "menu"}},
            # {{...}} templates are interpolated per-call (Phase 3): lazily synthesized at
            # call time, never prewarmed.
            "templ": {"type": "play", "prompt": "Hello {{caller_number}}!",
                      "next": {"default": "menu"}},
            # duplicate text across nodes synthesizes once.
            "greet2": {"type": "play", "prompt": "Thanks for calling Dream Team!",
                       "next": {"default": "menu"}},
            "vm": {"type": "voicemail", "greeting": "Leave a message after the tone."},
            "sales": {"type": "dial", "target": "+13055550000", "next": {}},
            "bye": {"type": "hangup"},
        },
    }
    texts = graph_prompt_texts(graph)
    check("play prompt collected", "Thanks for calling Dream Team!" in texts)
    check("menu media text collected", "Press 1 for sales, 2 for support." in texts)
    check("voicemail greeting collected", "Leave a message after the tone." in texts)
    check("sound: URI skipped", not any("owen/provisioned" in t for t in texts))
    check("prompt shadowed by a media URI skipped", "never played" not in texts)
    check("{{...}} template skipped", not any("{{" in t for t in texts))
    check("duplicates collapsed", len(texts) == 3)

    print("graph_prompt_texts — malformed graphs are safe no-ops:")
    check("non-dict graph -> []", graph_prompt_texts(None) == [])
    check("no nodes -> []", graph_prompt_texts({}) == [])
    check("non-dict node tolerated", graph_prompt_texts({"nodes": {"x": "junk"}}) == [])


def main():
    test_cache_key()
    test_is_media_uri()
    test_graph_prompt_texts()
    print("\nALL TTS PROMPT CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
