"""A number that may SEND the CRM's texts without being bound to the CRM (2026-10-09).

The CRM's automatic texts (appointment reminders, "submitted to AHS", "AHS authorized") go out
from +17869200331, whose CALLS the Retell agent answers. Binding it would re-route its calls to
the CRM's ring group, so it is listed on `CRM_LINK_SEND_ONLY_NUMBERS` instead:

  * the SMS route accepts it, riding the ONE enabled CRM binding (for the receipt marker);
  * its delivery receipts are relayed like a bound number's;
  * the CALL path never accepts it (`_bound_from_number` without `send_only`), and `resolve`
    — what the call path and inbound texts use — still answers None for it;
  * not listed, two enabled bindings, or the kill switch off: refused, never guessed.

Run: python -m tests.test_crm_send_only
"""

import asyncio
import uuid

BOUND_DID = "+19547758492"
SEND_ONLY = "+17869200331"
OTHER = "+17865550000"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_send_only failed at: {name}")


class FakeResult:
    def __init__(self, value=None, rows=None):
        self._value, self._rows = value, rows or []

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._value

    def all(self):
        return self._rows


def _number(phone):
    from app.models import Number

    return Number(phone_number=phone, media_provider="asterisk", owner_provider="bulkvs",
                  active=True, provider_status="Active", sms_enabled=True,
                  sms_campaign_id="CYZHJBQ")


class FakeSession:
    """Answers the three queries the binding code makes, by reading the SQL it is given."""

    def __init__(self, links=1):
        from app.integrations.crm.models import CrmLink

        self.bound_number = _number(BOUND_DID)
        self.links = [CrmLink(enabled=True, ring_operators=True, operator_ids=[],
                              pstn_numbers=[], number_id=uuid.uuid4()) for _ in range(links)]

    async def execute(self, stmt):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        if "crm_links.number_id" in sql.split("FROM")[0] and "JOIN" not in sql:
            return FakeResult(rows=[(link.number_id,) for link in self.links])
        if "JOIN" in sql and "crm_links" in sql:            # resolve(dialed)
            return FakeResult((self.links[0], self.bound_number)
                              if self.links and f"'{BOUND_DID}'" in sql else None)
        if "numbers" in sql:                                 # a Number by phone or id
            for phone in (SEND_ONLY, BOUND_DID, OTHER):
                if f"'{phone}'" in sql:
                    return FakeResult(_number(phone))
            return FakeResult(self.bound_number)             # by id: the bound line
        return FakeResult(None)


def _with(numbers="", enabled=True):
    from app.core.config import settings

    saved = (settings.CRM_LINK_ENABLED, settings.CRM_LINK_SEND_ONLY_NUMBERS)
    settings.CRM_LINK_ENABLED, settings.CRM_LINK_SEND_ONLY_NUMBERS = enabled, numbers
    return saved


def _restore(saved):
    from app.core.config import settings

    settings.CRM_LINK_ENABLED, settings.CRM_LINK_SEND_ONLY_NUMBERS = saved


def test_the_setting_is_parsed_on_the_last_ten_digits():
    print("CRM_LINK_SEND_ONLY_NUMBERS:")
    from app.integrations.crm import config as crm_config

    saved = _with("+1 786-920-0331, (786) 555-0000")
    try:
        keys = crm_config.current().send_only_numbers
    finally:
        _restore(saved)
    check("both numbers, as match keys", keys == {"7869200331", "7865550000"})
    saved = _with("")
    try:
        check("empty lists nothing", crm_config.current().send_only_numbers == frozenset())
    finally:
        _restore(saved)


def test_a_listed_number_rides_the_one_binding_and_others_do_not():
    print("resolve_sender:")
    from app.integrations.crm import binding

    saved = _with(SEND_ONLY)
    try:
        own = asyncio.run(binding.resolve_sender(FakeSession(), BOUND_DID))
        check("the bound line resolves to its own binding",
              own is not None and own.phone_number == BOUND_DID)
        rode = asyncio.run(binding.resolve_sender(FakeSession(), SEND_ONLY))
        check("the send-only number rides the bound line's binding",
              rode is not None and rode.phone_number == BOUND_DID)
        check("an unlisted number is refused",
              asyncio.run(binding.resolve_sender(FakeSession(), OTHER)) is None)
        check("two enabled bindings: never a guess",
              asyncio.run(binding.resolve_sender(FakeSession(links=2), SEND_ONLY)) is None)
        check("no binding at all: refused",
              asyncio.run(binding.resolve_sender(FakeSession(links=0), SEND_ONLY)) is None)
        check("the call / inbound path (resolve) still does not know it",
              asyncio.run(binding.resolve(FakeSession(), SEND_ONLY)) is None)
    finally:
        _restore(saved)
    saved = _with(SEND_ONLY, enabled=False)
    try:
        check("the kill switch refuses it",
              asyncio.run(binding.resolve_sender(FakeSession(), SEND_ONLY)) is None)
    finally:
        _restore(saved)


def test_the_sms_route_accepts_it_and_the_call_path_does_not():
    print("_bound_from_number:")
    from fastapi import HTTPException

    from app.integrations.crm import api as crm_api

    saved = _with(SEND_ONLY)
    try:
        number, bound = asyncio.run(crm_api._bound_from_number(FakeSession(), SEND_ONLY,
                                                               send_only=True))
        check("texting: the number itself is the sender",
              number.phone_number == SEND_ONLY and bound.phone_number == BOUND_DID)
        raised = None
        try:
            asyncio.run(crm_api._bound_from_number(FakeSession(), SEND_ONLY))
        except HTTPException as exc:
            raised = exc
        check("calling from it is refused 403", raised is not None and raised.status_code == 403)
    finally:
        _restore(saved)


if __name__ == "__main__":
    test_the_setting_is_parsed_on_the_last_ten_digits()
    test_a_listed_number_rides_the_one_binding_and_others_do_not()
    test_the_sms_route_accepts_it_and_the_call_path_does_not()
    print("crm_send_only: all passed")
