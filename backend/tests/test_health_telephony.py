"""Unit test for GET /health/telephony (BulkVS + Asterisk, ticket 01).

Mocks the ARI probes — NO live Asterisk required. Verifies: the disabled snapshot (flag
off never probes and never errors), the fully-up path, trunk-down while ARI is up, and
that an unreachable ARI degrades cleanly (trunk not probed, both false). Also exercises
the real asterisk_client probes against a refused port to prove they never raise.

Run: python -m tests.test_health_telephony
"""

import asyncio
import sys

from app.api import health
from app.core.config import settings
from app.providers import asterisk_client


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"health/telephony failed at: {name}")


def _const(value):
    async def _fn():
        return value
    return _fn


def _boom():
    async def _fn():
        raise AssertionError("probe must NOT be called when ASTERISK_ENABLED is off")
    return _fn


async def run():
    orig_reachable = asterisk_client.ari_reachable
    orig_trunk = asterisk_client.trunk_registered
    orig_flag = settings.ASTERISK_ENABLED
    try:
        print("telephony_health — flag-gated snapshot:")

        # 1. Flag off -> clean disabled snapshot; probes are never invoked.
        settings.ASTERISK_ENABLED = False
        asterisk_client.ari_reachable = _boom()
        asterisk_client.trunk_registered = _boom()
        result = await health.telephony_health()
        check("disabled snapshot reports all false, no error", result == {
            "asterisk_enabled": False, "ari_reachable": False, "trunk_registered": False,
        })

        # 2. Flag on, ARI reachable + trunk online -> everything true.
        settings.ASTERISK_ENABLED = True
        asterisk_client.ari_reachable = _const(True)
        asterisk_client.trunk_registered = _const(True)
        check("fully up reports all true", await health.telephony_health() == {
            "asterisk_enabled": True, "ari_reachable": True, "trunk_registered": True,
        })

        # 3. ARI reachable but trunk offline.
        asterisk_client.ari_reachable = _const(True)
        asterisk_client.trunk_registered = _const(False)
        check("ARI up, trunk down", await health.telephony_health() == {
            "asterisk_enabled": True, "ari_reachable": True, "trunk_registered": False,
        })

        # 4. ARI unreachable -> trunk is not probed and reported false.
        asterisk_client.ari_reachable = _const(False)
        asterisk_client.trunk_registered = _boom()  # must be skipped, not called
        check("ARI down skips trunk probe, both false", await health.telephony_health() == {
            "asterisk_enabled": True, "ari_reachable": False, "trunk_registered": False,
        })
    finally:
        asterisk_client.ari_reachable = orig_reachable
        asterisk_client.trunk_registered = orig_trunk
        settings.ASTERISK_ENABLED = orig_flag

    # 5. Real probes are best-effort: point ARI at a refused port, expect False not raise.
    orig_host, orig_port = settings.ARI_HOST, settings.ARI_PORT
    try:
        settings.ARI_HOST, settings.ARI_PORT = "127.0.0.1", 1  # nothing listens here
        print("asterisk_client probes — best-effort (never raise):")
        check("ari_reachable() returns False on connection refused",
              await asterisk_client.ari_reachable() is False)
        check("trunk_registered() returns False on connection refused",
              await asterisk_client.trunk_registered() is False)
    finally:
        settings.ARI_HOST, settings.ARI_PORT = orig_host, orig_port

    print("\nALL HEALTH/TELEPHONY CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except SystemExit as e:
        print(e)
        sys.exit(1)
