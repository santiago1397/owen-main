"""No test reaches Retell. Imported (and `install()`ed) by every Retell test module.

The rule (docs/RETELL-PLAN.md, "Safety"): a test that ends up opening a connection to
api.retellai.com or sip.retellai.com — because a mock was forgotten, a transport not passed,
a setting left pointing at the real API — must FAIL, not quietly make a real request with
whatever key happens to be in the environment. So the lookup itself is refused, at the socket
layer every client goes through (`socket.getaddrinfo`, which httpx/anyio, requests and a raw
socket all call), and recorded; `assert_untouched()` at the end of the module turns any
recorded attempt into a failure even if the code under test swallowed the refusal — which the
engine does by design (every failure is the `failed` port).

Also refused: whatever host `RETELL_API_BASE` points at, so a test cannot dodge the guard by
changing the setting.

This is the guard; it is not a substitute for mocking at the HTTP boundary
(`integrations/retell/client.TRANSPORT`), which is how the tests talk to "Retell".
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

GUARDED_SUFFIX = "retellai.com"

attempts: list[str] = []
_original_getaddrinfo = socket.getaddrinfo
_installed = False


class RetellRefused(OSError):
    pass


def _extra_hosts() -> set[str]:
    try:
        from app.core.config import settings

        host = urlparse(str(settings.RETELL_API_BASE or "")).hostname or ""
        sip = str(settings.RETELL_SIP_HOST or "")
        return {h.lower() for h in (host, sip) if h}
    except Exception:  # noqa: BLE001
        return set()


def refused(host) -> bool:
    name = str(host or "").lower().rstrip(".")
    return (name == GUARDED_SUFFIX or name.endswith("." + GUARDED_SUFFIX)
            or name in _extra_hosts())


def _guarded_getaddrinfo(host, *args, **kwargs):
    # anyio (under httpx) passes the host as bytes; a raw socket passes str.
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    if refused(name):
        attempts.append(name)
        raise RetellRefused(f"tests may not reach Retell (refused lookup of {name})")
    return _original_getaddrinfo(host, *args, **kwargs)


def _fail_at_exit() -> None:
    """Whatever the module's own checks said, a recorded attempt fails the run. A test that
    triggers the guard on purpose clears `attempts` after asserting on it."""
    if attempts:
        import os
        import sys

        sys.stdout.write(f"\nRETELL GUARD: a test tried to reach Retell: {attempts}\n")
        sys.stdout.flush()
        os._exit(1)


def install() -> None:
    """Installed for EVERY test module by `tests/__init__.py` (each `python -m tests.<name>`
    imports the package first), so a module that never heard of Retell is guarded too."""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _guarded_getaddrinfo
    import atexit

    atexit.register(_fail_at_exit)
    _installed = True


def assert_untouched() -> None:
    if attempts:
        raise SystemExit(f"RETELL GUARD: a test tried to reach Retell: {attempts}")
    print("  [PASS] the Retell guard saw no attempt to reach Retell")
