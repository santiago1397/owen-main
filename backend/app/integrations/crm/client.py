"""HTTP client for the CRM (`ghl-clone`). Runs in the APP container only.

## Reachability, from the compose files rather than from hope

`callmon_app` is on `[callmon-net, traefik-public]`; `ghl_clone_api` is on
`[ghl-net, traefik-public]`. `traefik-public` is declared `external: true` in both stacks,
so it is the SAME docker network and `http://ghl_clone_api:8000` resolves over it by
container name — internal, no DNS round trip to the internet, and nothing new exposed
publicly. (`callmon_worker` is on `callmon-net` ONLY, which is why the report job hops
through the app; see `push.py`.)

This was read out of `docker-compose.prod.yml` on both sides. It has NOT been confirmed by
an actual request from inside a running container — the sandbox this was built in has no
docker access. Recorded as a known unknown in `.qa/state/crmlink-done`; `probe()` below is
the one command that settles it.

## Contact resolution, and why it is fiddly

`POST /api/events` files an event against a `contact_id` when it is given one, and 404s if
that id does not exist. Naming the exact contact is still the best answer OWEN can give, so
it looks one up first — and two verified obstacles make that harder than it sounds:

  1. **Phone format.** `Contact.phone` is a display string — the CRM's own seed writes
     `"(941) 555-1234"`. OWEN holds E.164. `GET /api/contacts?q=+19415551234` ILIKEs the raw
     string and matches nothing, ever. So several renderings are tried, and every candidate
     is then confirmed by comparing the LAST TEN DIGITS.
  2. **Scope.** A token scoped `events:write` alone can POST /api/events and nothing else —
     `auth._scope_allows` gates GET on `{read, write, admin}`. Contact lookup therefore
     needs a token carrying **`events:write read`**. With a write-only token, lookup 403s
     and this module reports that clearly instead of guessing an id.

A caller with no matching contact is NOT created here, and never will be. But the event is
no longer DROPPED for it either: since the CRM's 2026-09-11 amendment the body carries
`from_number`, and the CRM matches-or-creates on its own side, where the new-lead automation
and the duplicate guard live. That is the right place for it — creating contacts from here
is precisely the "hundreds of junk contacts" failure `docs/CRM_CONTEXT_SPEC.md` C10 records.
So this function returning `(None, reason)` now means "we could not name the contact", not
"throw the call away"; see `events.py`, THE AMENDMENT.

Match is EXACT on the last ten digits, never fuzzy. Filing a call on the wrong customer's
timeline is worse than filing it nowhere — the same judgement C3 made about greeting the
wrong person by name.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.integrations.crm.config import digits, match_key

logger = logging.getLogger("integrations.crm.client")

EVENTS_PATH = "/api/events"
DELIVERY_PATH = "/api/events/delivery"
CONTACTS_PATH = "/api/contacts"
HEALTH_PATH = "/api/health"

# How many contacts a lookup will look at per candidate rendering before giving up. The
# search is a substring ILIKE, so a short candidate can match broadly; the last-ten-digits
# confirmation below is what makes a wide net safe.
_LOOKUP_PAGE_SIZE = 50


@dataclass(frozen=True)
class CrmResult:
    ok: bool
    status: int = 0
    reason: str = ""
    data: Optional[dict] = None

    @property
    def retryable(self) -> bool:
        """Whether the caller should raise so the job queue retries.

        A 5xx, a timeout or a transport error is worth retrying: the CRM may simply be
        restarting. A 4xx is not — a 401 means the token is wrong, a 404 means the contact
        does not exist, and neither improves by being asked again five times.
        """
        return self.status == 0 or self.status >= 500


def phone_candidates(number: str) -> list[str]:
    """Search strings to try for one phone number, most specific first.

    Ordered so the narrowest query runs first and usually terminates the search on its first
    page. The 7-digit `NNN-NNNN` form is the one that actually matches this CRM's stored
    `"(941) 555-1234"`; the rest are there because the CRM's storage format is a convention,
    not a constraint, and a contact typed in by hand may hold anything.
    """
    d = digits(number)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    out: list[str] = []
    if len(d) == 10:
        area, exch, last = d[:3], d[3:6], d[6:]
        out.extend([
            f"{exch}-{last}",             # matches "(941) 555-1234" and "941-555-1234"
            f"({area}) {exch}-{last}",
            f"{area}{exch}{last}",        # matches an unformatted 10-digit store
            f"+1{d}",                     # matches an E.164 store
        ])
    elif d:
        out.append(d)
    # De-duplicate, keep order.
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


class CrmClient:
    """One CRM, one machine token. Construct per request — httpx clients are cheap and a
    long-lived one would outlive a token rotation.

    ## The total budget, and why it is not optional

    `workers/handlers.py::handle_crm_report` posts to this module's adapter with
    `httpx.AsyncClient(timeout=20)`. One delivery can make up to FIVE calls out to the CRM
    (four contact-lookup renderings plus the event POST), so a per-request timeout alone is
    not a bound: at 15s each, a slow CRM would blow through the worker's 20s and the job
    would be retried — after the CRM may already have recorded the event. `POST /api/events`
    always INSERTS (it has no dedupe on `provider_ref`), so that retry is a duplicate row on
    a customer's timeline.

    So the whole delivery gets ONE budget, and each request takes the smaller of its own
    timeout and what is left of it. `budget_s` defaults comfortably inside the worker's 20s.
    """

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 5.0,
                 budget_s: float = 15.0) -> None:
        self.base = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = float(timeout_s or 5.0)
        self.budget = float(budget_s or 15.0)
        self._deadline = time.monotonic() + self.budget

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    def _headers(self) -> dict[str, str]:
        # Bearer, per ghl-clone `auth.current_principal`: a header beginning `ghl_pat_` is
        # resolved as a machine token. A cookie session is not an option for a service.
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> CrmResult:
        if not self.base:
            return CrmResult(False, 0, "no CRM base URL configured")
        if not self.token:
            return CrmResult(False, 0, "no CRM token configured")
        remaining = self._remaining()
        if remaining <= 0:
            # Reported as a transport failure (status 0), which `CrmResult.retryable` treats
            # as retryable — the right answer: we never reached the CRM, so nothing was
            # recorded and asking again is safe.
            logger.warning("crm-link: %s %s skipped — the %.0fs delivery budget is spent",
                           method, path, self.budget)
            return CrmResult(False, 0, "delivery budget exhausted before the request")
        url = f"{self.base}{path}"
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, remaining)) as client:
                resp = await client.request(method, url, headers=self._headers(), **kwargs)
        except Exception as exc:  # noqa: BLE001 - transport failure is retryable, not fatal
            logger.warning("crm-link: %s %s failed: %r", method, url, exc)
            return CrmResult(False, 0, f"transport error: {exc!r}")
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            snippet = (str(body) or "").replace("\n", " ")[:300]
            return CrmResult(False, resp.status_code, snippet,
                             body if isinstance(body, dict) else None)
        return CrmResult(True, resp.status_code, "",
                         body if isinstance(body, dict) else {"data": body})

    async def probe(self) -> CrmResult:
        """`GET /api/health` — the one call that proves container-name reachability, auth
        aside (health is in the CRM's `auth.EXEMPT` set, so it answers unauthenticated)."""
        return await self._request("GET", HEALTH_PATH)

    async def resolve_contact_id(self, phone: str) -> tuple[Optional[int], str]:
        """`(contact_id, reason)`. `contact_id` is None when nothing matched exactly.

        `reason` always says WHY — "no contact matches +1...", "token lacks the 'read'
        scope", "CRM unreachable" — because the caller writes it into a log line that is
        the only trace of a call that did not reach the CRM.
        """
        target = match_key(phone)
        if not target:
            return None, "caller number is empty or unusable"

        for candidate in phone_candidates(phone):
            result = await self._request(
                "GET", CONTACTS_PATH,
                params={"q": candidate, "page_size": _LOOKUP_PAGE_SIZE, "page": 1},
            )
            if not result.ok:
                if result.status == 403:
                    return None, (
                        "the CRM token cannot read contacts — POST /api/events needs a "
                        "contact_id, so this token needs the 'read' scope as well as "
                        "'events:write'"
                    )
                if result.status == 401:
                    return None, "the CRM rejected the token (401)"
                # A transport error or 5xx: stop trying renderings and let the caller retry
                # the whole job rather than hammering the CRM with four more queries.
                return None, f"contact lookup failed: {result.reason}"
            items = (result.data or {}).get("items")
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                # EXACT last-ten confirmation. The ILIKE that found this row is a substring
                # match and will happily return a different customer whose number contains
                # the same seven digits.
                if match_key(item.get("phone")) == target and item.get("id") is not None:
                    try:
                        return int(item["id"]), "matched"
                    except (TypeError, ValueError):
                        continue
        return None, f"no CRM contact matches {phone}"

    async def post_delivery_receipt(self, body: dict) -> CrmResult:
        """`POST /api/events/delivery`. Same `events:write` scope as the event ingest, so a
        receipt needs no second credential — build the body with
        `events.to_crm_delivery_receipt` and check it with `validate_crm_delivery_receipt`.

        A **404** here is a real answer, not a failure: it means the CRM has no outbound
        message with that `provider_ref`, which is what a receipt for a text sent before the
        link existed looks like. The caller completes the job rather than retrying it.
        """
        result = await self._request("POST", DELIVERY_PATH, json=body)
        if result.ok:
            logger.info("crm-link: delivery receipt applied (ref=%s status=%s advanced=%s)",
                        body.get("provider_ref"), body.get("status"),
                        (result.data or {}).get("advanced"))
        else:
            logger.warning("crm-link: delivery receipt REFUSED by the CRM (%s): %s",
                           result.status, result.reason)
        return result

    async def post_event(self, body: dict) -> CrmResult:
        """`POST /api/events`. The body must already be in the CRM's shape — build it with
        `events.to_crm_event` and check it with `events.validate_crm_event`."""
        result = await self._request("POST", EVENTS_PATH, json=body)
        if result.ok:
            logger.info("crm-link: event delivered (contact=%s type=%s status=%s)",
                        body.get("contact_id"), body.get("type"), result.status)
        else:
            logger.warning("crm-link: event REFUSED by the CRM (%s): %s",
                           result.status, result.reason)
        return result
