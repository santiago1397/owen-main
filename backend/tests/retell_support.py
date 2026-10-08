"""Fakes for the Retell tests: an in-memory registry, a fake ARI, a mocked Retell, settings.

Nothing here opens a socket or a database. Retell is mocked at the HTTP boundary
(`integrations/retell/client.TRANSPORT` = an `httpx.MockTransport`), so the engine's real
request-building and response-handling code runs.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone

from tests import retell_guard

retell_guard.install()

KEY = "key_test_retell_not_real"
CALLER = "+19415550123"
DID = "+19545550199"
LINKEDID = "1759780000.17"
CHANNEL = "chan-caller-1"


def check(name, cond, module="retell"):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"{module} failed at: {name}")


class MemoryRegistry:
    """`integrations/retell/registry.DbRegistry`, in a dict. Same answers, same once-only
    event claim, so the engine, functions and webhook run their real logic against it."""

    def __init__(self, fail_create: bool = False):
        self.rows: dict[str, dict] = {}
        self.fail_create = fail_create
        self.calls: list[str] = []

    def _snap(self, call_id):
        row = self.rows.get(call_id)
        return copy.deepcopy(row) if row is not None else None

    async def create(self, **fields):
        self.calls.append("create")
        if self.fail_create:
            raise RuntimeError("database down")
        row = {"status": "live", "exit_port": None, "exit_data": None, "captured": None,
               "requests": None, "ended_event_at": None, "analyzed_event_at": None,
               "created_at": datetime.now(timezone.utc)}
        row.update(fields)
        self.rows[fields["retell_call_id"]] = row

    async def update(self, call_id, **fields):
        if call_id in self.rows:
            self.rows[call_id].update(fields)

    async def get(self, call_id):
        return self._snap(call_id)

    async def live(self):
        return [copy.deepcopy(r) for r in self.rows.values() if r.get("status") == "live"]

    async def live_for(self, linkedid):
        for r in await self.live():
            if r.get("linkedid") == linkedid:
                return r
        return None

    async def exit_request(self, call_id):
        row = self.rows.get(call_id)
        if not row or not row.get("exit_port"):
            return None
        return row["exit_port"], dict(row.get("exit_data") or {})

    async def transfer_tried(self, linkedid, *, exclude_call_id=""):
        return any(r.get("linkedid") == linkedid and cid != exclude_call_id
                   and r.get("exit_port") == "transfer" for cid, r in self.rows.items())

    async def request_exit(self, call_id, port, data):
        row = self.rows.get(call_id)
        if not row or row.get("status") != "live" or row.get("exit_port"):
            return False
        row["exit_port"], row["exit_data"] = port, dict(data)
        return True

    async def merge_capture(self, call_id, fields):
        row = self.rows.get(call_id)
        if not row:
            return False
        merged = dict(row.get("captured") or {})
        merged.update(fields)
        row["captured"] = merged
        return True

    async def add_request(self, call_id, request):
        row = self.rows.get(call_id)
        if row is not None:
            existing = list(row.get("requests") or [])
            if request not in existing:
                row["requests"] = existing + [dict(request)]

    async def finish(self, call_id):
        row = self.rows.get(call_id)
        if row is None:
            return None
        row["status"] = "ended"
        return self._snap(call_id)

    async def claim_event(self, call_id, kind, fields):
        row = self.rows.get(call_id)
        col = {"ended": "ended_event_at", "analyzed": "analyzed_event_at"}[kind]
        if row is None or row.get(col) is not None:
            return None
        row.update(fields)
        row[col] = datetime.now(timezone.utc)
        return self._snap(call_id)

    async def release_event(self, call_id, kind):
        col = {"ended": "ended_event_at", "analyzed": "analyzed_event_at"}[kind]
        if call_id in self.rows:
            self.rows[call_id][col] = None


class FakeAri:
    """The ARI surface the Retell engine uses. Records every operation. `answer` is what
    the Retell leg does; `script` is a coroutine run once the leg is bridged, to play the
    part of the world (Retell hanging up, the caller hanging up, a function call ...)."""

    def __init__(self, answer="answered", bridge_ok=True, script=None):
        self.answer = answer
        self.bridge_ok = bridge_ok
        self.script = script
        self.ops: list[tuple] = []
        self.endpoint = None
        self.out_id = None

    async def originate_sip_leg(self, queue, channel_id, out_id, endpoint, *, timeout_s):
        self.ops.append(("originate", endpoint))
        self.endpoint, self.out_id = endpoint, out_id
        return self.answer

    async def create_bridge(self):
        self.ops.append(("create_bridge",))
        return "bridge-1"

    async def add_to_bridge(self, bridge_id, *channels):
        self.ops.append(("add_to_bridge", bridge_id, channels))
        return self.bridge_ok

    async def record_bridge(self, bridge_id, name):
        self.ops.append(("record_bridge", bridge_id, name))
        if self.script is not None:
            asyncio.get_running_loop().create_task(self.script(self))

    async def hangup(self, channel_id):
        self.ops.append(("hangup", channel_id))

    async def destroy_bridge(self, bridge_id):
        self.ops.append(("destroy_bridge", bridge_id))

    def names(self):
        return [o[0] for o in self.ops]


def leg_gone(channel_id: str) -> dict:
    return {"type": "ChannelDestroyed", "channel": {"id": channel_id}, "cause": 16}


class MockRetell:
    """`httpx.MockTransport` standing in for api.retellai.com. Records every request."""

    def __init__(self, status=200, body=None, raise_exc=None):
        import httpx

        self.status = status
        self.body = body if body is not None else {"call_id": "call_abc123"}
        self.raise_exc = raise_exc
        self.requests: list = []

        def handler(request: httpx.Request):
            self.requests.append(request)
            if self.raise_exc is not None:
                raise self.raise_exc
            return httpx.Response(self.status, json=self.body)

        self.transport = httpx.MockTransport(handler)

    def sent_json(self, i=0) -> dict:
        return json.loads(self.requests[i].content)


class Settings:
    """Override settings for a block, restoring them after."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.saved = {}

    def __enter__(self):
        from app.core.config import settings

        self.settings = settings
        for k, v in self.overrides.items():
            self.saved[k] = getattr(settings, k)
            setattr(settings, k, v)
        return settings

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.settings, k, v)
        return False


class Patch:
    """Set attributes on objects for a block, restoring them after."""

    def __init__(self, *triples):
        self.triples = triples
        self.saved = []

    def __enter__(self):
        for obj, name, value in self.triples:
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        return False
