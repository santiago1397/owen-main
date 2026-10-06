"""Retell voice agents — OWEN keeps the call, Retell holds the conversation (2026-10-06).

Read `docs/RETELL-PLAN.md` first: it records the owner's decisions and the contract with the
CRM (C1-C7). This package is OWEN's half of it.

## The call path (decision 2)

    BulkVS -> Asterisk -> OWEN's flow -> ai_agent node (engine "retell")
        1. POST https://api.retellai.com/v2/register-phone-call   (client.py)
              -> Retell hands back a call_id; it is persisted at once (registry.py)
        2. ARI originates PJSIP/<RETELL_SIP_ENDPOINT>/sip:<call_id>@sip.retellai.com
        3. the caller and the Retell leg are bridged and the BRIDGE is recorded here
        4. the node blocks until the Retell leg hangs up, the caller hangs up, a function
           call asks to leave (transfer / end_call), or a supervisor takes over
    Any failure on the way is the `failed` port -> the flow's fallback (voicemail).

The engine itself is `app/agents/retell.py`, beside the other engines.

## Retell talks back to OWEN over HTTPS (api.py)

    POST /api/retell/webhook            call_started | call_ended | call_analyzed
    POST /api/retell/functions/{name}   transfer | end_call | capture_lead | request_change

PUBLIC routes on `api.<APP_DOMAIN>` — Retell is on the internet — so every request is
verified with Retell's signature (signature.py) BEFORE anything is read or done: 401 for a
bad, stale or missing signature, 503 while RETELL_API_KEY is unset.

## Off unless configured

No RETELL_API_KEY: the engine takes the `failed` port without a request, both routes answer
503, and nothing anywhere talks to Retell. No test reaches Retell either: `tests/retell_guard.py`
refuses any connection to api.retellai.com / sip.retellai.com and fails the test that tried.
"""
