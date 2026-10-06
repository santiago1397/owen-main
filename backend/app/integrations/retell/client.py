"""The ONE place OWEN sends a request to Retell: `POST /v2/register-phone-call`.

Retell's "dial to SIP URI" method (decision 2): register the call, get a `call_id`, then dial
`sip:<call_id>@sip.retellai.com`. Nothing else is ever requested from Retell — no agent edits,
no phone-number purchases, no outbound calls. The CRM shows Retell's settings read-only and
Retell's dashboard owns them (decision 1).

## The version that answers (decision 14)

`agent_version` is OMITTED unless the agent version pins `retell_agent_version`, so Retell
picks the version itself. Retell documents an omitted version as "the latest version"; whether
that means the latest PUBLISHED version or the draft has NOT been verified against a real
account — the owner checks it on the pilot DID (phase 3) before real traffic, and the version
that actually answered is recorded from the `call_ended` webhook either way.

## Failure

Any failure — no key, transport error, timeout, non-2xx, a body without `call_id` — raises
`RetellError` with a sentence that never contains the key or a customer's number. The engine
turns that into the `failed` port.

`TRANSPORT` exists for tests: an `httpx.MockTransport` there means no socket is opened. It is
the HTTP-boundary seam, the same idea as the CRM's AI providers' `HTTP_CLIENT_FACTORY`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("integrations.retell.client")

REGISTER_PATH = "/v2/register-phone-call"

# Test seam: an httpx transport to use instead of the network. None in production.
TRANSPORT = None


class RetellError(Exception):
    """Registration failed. `str(exc)` is safe to log."""


def register_body(*, agent_id: str, from_number: str, to_number: str, metadata: dict,
                  variables: dict, agent_version: int | None = None) -> dict:
    """The request body. PURE, so its shape is pinned by a test without a network."""
    body = {
        "agent_id": str(agent_id),
        "from_number": str(from_number or ""),
        "to_number": str(to_number or ""),
        "direction": "inbound",
        "metadata": dict(metadata or {}),
        "retell_llm_dynamic_variables": {str(k): str(v) for k, v in (variables or {}).items()},
    }
    if agent_version is not None:
        body["agent_version"] = int(agent_version)
    return body


async def register_phone_call(body: dict) -> dict:
    """POST the registration; return Retell's call object (it carries `call_id`)."""
    import httpx

    from app.core.config import settings

    key = (settings.RETELL_API_KEY or "").strip()
    if not key:
        raise RetellError("RETELL_API_KEY is not set")
    base = (settings.RETELL_API_BASE or "https://api.retellai.com").rstrip("/")
    timeout = httpx.Timeout(float(settings.RETELL_REGISTER_TIMEOUT_SECONDS or 3.0))
    kwargs = {"timeout": timeout}
    if TRANSPORT is not None:
        kwargs["transport"] = TRANSPORT
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(
                f"{base}{REGISTER_PATH}", json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
    except httpx.TimeoutException:
        raise RetellError("Retell did not answer register-phone-call in time") from None
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same outcome
        raise RetellError(f"could not reach Retell ({type(exc).__name__})") from None
    if resp.status_code >= 400:
        # Status and a short, flattened reason. Retell's error bodies echo the request's
        # fields, which include the caller's number, so the body itself is not logged.
        raise RetellError(f"Retell refused register-phone-call with HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise RetellError("Retell answered register-phone-call with something not JSON") from None
    if not isinstance(data, dict) or not str(data.get("call_id") or "").strip():
        raise RetellError("Retell's register-phone-call answer has no call_id")
    return data
