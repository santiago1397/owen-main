"""Unit tests for the operator WebRTC softphone (Ticket 13).

Dependency-free by design (like test_flow_interpreter): exercises the PURE
app.telephony.credentials (SIP + ephemeral TURN cred minting) and app.telephony.control (the
server-side ARI control ORCHESTRATION — bridge/hold/blind-transfer) with a FAKE ARI client.
It does NOT import fastapi/httpx/pydantic (absent in the sandbox), so it does not exercise the
thin FastAPI wrappers in app/api/telephony.py directly.

Asserts:
- TURN creds are ephemeral + verifiable: username embeds a future unix expiry, credential ==
  base64(HMAC-SHA1(secret, username)); empty secret => no creds; different operators/expiries
  yield different creds.
- build_webrtc_credentials returns a SHORT-LIVED SIP block (expires_at == now + ttl), the
  per-operator endpoint name, and ice_servers only when TURN is configured.
- control.resolve_transfer_endpoint maps did/operator/ai_agent correctly and rejects junk.
- control hold/unhold/bridge/blind_transfer drive exactly the right ARI ops in order (fake).

NOTE (unrun here): the AUTHORIZATION of the minting + control endpoints (Depends(current_user))
and the ASTERISK_ENABLED gate live in app/api/telephony.py, which needs fastapi — not importable
in this sandbox. Those are asserted structurally (endpoints declare `user: User = Depends(
current_user)` and call `_require_enabled()`); run them under the full backend venv.

Run: python -m tests.test_webrtc_credentials
"""

import base64
import hashlib
import hmac
import sys

from app.telephony import control
from app.telephony.credentials import (
    build_webrtc_credentials,
    mint_turn_credentials,
    operator_dial_endpoint,
    operator_endpoint_name,
    operator_slug,
)


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"webrtc_credentials failed at: {name}")


NOW = 1_700_000_000  # fixed clock for determinism
SECRET = "turn-shared-secret"


def test_operator_naming():
    print("operator slug / endpoint naming is deterministic + SIP-safe:")
    check("email -> safe slug", operator_slug("Jane.Doe@Example.com") == "jane.doe-example.com")
    check("endpoint name is operator-<slug>", operator_endpoint_name("a@b.com") == "operator-a-b.com")
    check("dial endpoint is PJSIP/operator-<slug>", operator_dial_endpoint("a@b.com") == "PJSIP/operator-a-b.com")
    check("blank id degrades to 'unknown'", operator_slug("   ") == "unknown")


def test_turn_creds_are_ephemeral_and_verifiable():
    print("TURN creds — ephemeral use-auth-secret (coturn REST API):")
    c = mint_turn_credentials(SECRET, "op@x.com", 3600, now=NOW)
    check("username embeds the unix expiry", c["username"] == f"{NOW + 3600}:op-x.com")
    check("ttl echoed", c["ttl"] == 3600 and c["expires_at"] == NOW + 3600)
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), c["username"].encode(), hashlib.sha1).digest()
    ).decode()
    check("credential == base64(HMAC-SHA1(secret, username))", c["credential"] == expected)

    later = mint_turn_credentials(SECRET, "op@x.com", 3600, now=NOW + 5)
    check("later mint -> different username+credential (self-expiring)",
          later["username"] != c["username"] and later["credential"] != c["credential"])
    other = mint_turn_credentials(SECRET, "other@x.com", 3600, now=NOW)
    check("different operator -> different creds", other["credential"] != c["credential"])
    empty = mint_turn_credentials("", "op@x.com", 3600, now=NOW)
    check("no secret -> empty creds (TURN disabled)", empty["credential"] == "" and empty["username"] == "")


def test_build_webrtc_credentials_short_lived():
    print("build_webrtc_credentials — short-lived SIP + TURN ice_servers:")
    blob = build_webrtc_credentials(
        operator_id="op@x.com",
        sip_secret="sip-pass",
        sip_domain="api.owen.test",
        wss_url="wss://api.owen.test/ws",
        turn_secret=SECRET,
        turn_urls=["turns:turn.owen.test:443?transport=tcp"],
        sip_ttl_seconds=1800,
        turn_ttl_seconds=1800,
        now=NOW,
    )
    sip = blob["sip"]
    check("endpoint = operator-<slug>", sip["endpoint"] == "operator-op-x.com")
    check("auth username == endpoint", sip["authorization_username"] == "operator-op-x.com")
    check("SIP is short-lived (expires_at == now + ttl)", sip["expires_at"] == NOW + 1800)
    check("password + wss + domain passed through",
          sip["password"] == "sip-pass" and sip["wss_url"] == "wss://api.owen.test/ws"
          and sip["domain"] == "api.owen.test")
    check("one ice_server with TURN url + ephemeral creds",
          len(blob["ice_servers"]) == 1
          and blob["ice_servers"][0]["urls"] == ["turns:turn.owen.test:443?transport=tcp"]
          and blob["ice_servers"][0]["username"].endswith(":op-x.com"))

    # No TURN configured -> no ice_servers (STUN/host candidates only).
    no_turn = build_webrtc_credentials(
        operator_id="op@x.com", sip_secret="p", sip_domain="d", wss_url="w",
        turn_secret="", turn_urls=[], sip_ttl_seconds=60, turn_ttl_seconds=60, now=NOW,
    )
    check("no TURN urls -> empty ice_servers", no_turn["ice_servers"] == [])


def test_resolve_transfer_endpoint():
    print("blind-transfer target resolution (did / operator / ai_agent):")
    check("did -> PJSIP/<num>@<trunk>",
          control.resolve_transfer_endpoint("did", "+13055550000", trunk_name="bulkvs")
          == "PJSIP/+13055550000@bulkvs")
    check("operator -> PJSIP/operator-<slug>",
          control.resolve_transfer_endpoint("operator", "jane@x.com", trunk_name="bulkvs")
          == "PJSIP/operator-jane-x.com")
    check("ai_agent -> Local/<id>@owen-agents",
          control.resolve_transfer_endpoint("ai_agent", "agent7", trunk_name="bulkvs")
          == "Local/agent7@owen-agents")
    check("unknown kind -> None", control.resolve_transfer_endpoint("bogus", "x", trunk_name="bulkvs") is None)
    check("empty target -> None", control.resolve_transfer_endpoint("did", "", trunk_name="bulkvs") is None)
    check("all kinds enumerated", control.TRANSFER_KINDS == frozenset({"did", "operator", "ai_agent"}))


class FakeAri:
    """Records every backend control op; scriptable bridge id."""

    def __init__(self, bridge_id="bridge-1"):
        self.ops = []
        self._bridge_id = bridge_id

    async def hold(self, channel_id):
        self.ops.append(("hold", channel_id))

    async def unhold(self, channel_id):
        self.ops.append(("unhold", channel_id))

    async def create_bridge(self):
        self.ops.append(("create_bridge", None))
        return self._bridge_id

    async def add_to_bridge(self, bridge_id, *channel_ids):
        self.ops.append(("add_to_bridge", (bridge_id, channel_ids)))

    async def destroy_bridge(self, bridge_id):
        self.ops.append(("destroy_bridge", bridge_id))

    async def blind_transfer(self, channel_id, endpoint):
        self.ops.append(("blind_transfer", (channel_id, endpoint)))


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_control_orchestration_drives_right_ari_ops():
    print("control orchestration drives the right ARI ops (fake client):")
    ari = FakeAri()
    _run(control.hold(ari, "chan-A"))
    _run(control.unhold(ari, "chan-A"))
    check("hold then unhold on the channel",
          ari.ops == [("hold", "chan-A"), ("unhold", "chan-A")])

    ari = FakeAri(bridge_id="bridge-9")
    bid = _run(control.bridge(ari, "chan-op", "chan-caller"))
    check("bridge returns the created id", bid == "bridge-9")
    check("bridge creates then adds BOTH legs",
          ari.ops == [("create_bridge", None), ("add_to_bridge", ("bridge-9", ("chan-op", "chan-caller")))])

    # create_bridge failing -> None, no addChannel attempted.
    fail = FakeAri(bridge_id=None)
    check("bridge failure -> None", _run(control.bridge(fail, "a", "b")) is None)
    check("no addChannel on failed bridge", fail.ops == [("create_bridge", None)])

    ari = FakeAri()
    ep = _run(control.blind_transfer(ari, "chan-caller", "did", "+13055550000", trunk_name="bulkvs"))
    check("blind_transfer redirects to the resolved endpoint",
          ep == "PJSIP/+13055550000@bulkvs"
          and ari.ops == [("blind_transfer", ("chan-caller", "PJSIP/+13055550000@bulkvs"))])

    bad = FakeAri()
    check("blind_transfer with bad target -> None, no ARI op",
          _run(control.blind_transfer(bad, "c", "bogus", "x", trunk_name="bulkvs")) is None
          and bad.ops == [])


def main():
    test_operator_naming()
    test_turn_creds_are_ephemeral_and_verifiable()
    test_build_webrtc_credentials_short_lived()
    test_resolve_transfer_endpoint()
    test_control_orchestration_drives_right_ari_ops()
    print("\nALL WEBRTC CREDENTIAL + CONTROL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
