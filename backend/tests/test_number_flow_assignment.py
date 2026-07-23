"""Unit test for the number↔flow assignment guard (app.flows.service, Ticket 15.5).

Dependency-free (like test_flow_versioning): `flow_assignment_error` is the pure kernel
the PATCH /api/numbers/{id} endpoint applies before setting `numbers.flow_id`. The endpoint
loads the rows; this function decides — so the guard rules are proven without FastAPI/DB.

Asserts:
- a flow with an active version may be assigned to an asterisk-media number (None = ok);
- non-asterisk media (Twilio/SignalWire legacy: None or another provider) -> error;
- a missing flow -> error;
- a flow with NO active version (drafts only) -> error;
- guard precedence: the media check fires first (a bad number never leaks flow state).

Run: python -m tests.test_number_flow_assignment
"""

import sys

from app.flows.service import flow_assignment_error

EXPECTED = "asterisk"  # mirrors settings.BULKVS_MEDIA_PROVIDER's default


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"number_flow_assignment failed at: {name}")


def main():
    print("flow_assignment_error — allowed assignment:")
    check("asterisk-media number + active flow -> ok (None)", flow_assignment_error(
        number_media_provider="asterisk", expected_media_provider=EXPECTED,
        flow_exists=True, flow_active_version_id="some-uuid",
    ) is None)

    print("flow_assignment_error — media-provider guard (15.5):")
    check("legacy number (media_provider None) -> error", flow_assignment_error(
        number_media_provider=None, expected_media_provider=EXPECTED,
        flow_exists=True, flow_active_version_id="some-uuid",
    ) is not None)
    check("non-asterisk media -> error", flow_assignment_error(
        number_media_provider="twilio", expected_media_provider=EXPECTED,
        flow_exists=True, flow_active_version_id="some-uuid",
    ) is not None)

    print("flow_assignment_error — flow guards (15.5):")
    check("missing flow -> error", flow_assignment_error(
        number_media_provider="asterisk", expected_media_provider=EXPECTED,
        flow_exists=False, flow_active_version_id=None,
    ) == "flow not found")
    check("flow without active version -> error mentioning activation", "active version" in (
        flow_assignment_error(
            number_media_provider="asterisk", expected_media_provider=EXPECTED,
            flow_exists=True, flow_active_version_id=None,
        ) or ""))

    print("flow_assignment_error — precedence:")
    err = flow_assignment_error(
        number_media_provider="twilio", expected_media_provider=EXPECTED,
        flow_exists=False, flow_active_version_id=None,
    )
    check("media check fires before flow checks", err is not None and "media_provider" in err)

    print("\nALL NUMBER FLOW ASSIGNMENT CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
