"""An Asterisk recording (every AGENT call's) is reported to the CRM once it is on disk
(2026-10-08). The asterisk branch of recording_fetch used to return before the CRM report,
so no agent call ever got its player in the CRM.

Run: python -m tests.test_agent_recording_reaches_crm
"""
import asyncio
import os
import sys
import tempfile
import uuid

from app.workers import handlers

FAILS = []


def check(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


class Rec:
    def __init__(self, path):
        self.id, self.storage_path, self.transcribed = uuid.uuid4(), path, False
        self.provider_recording_sid = "1791490686.30-agent-1"


class Db:
    def __init__(self, rec):
        self.rec = rec

    async def get(self, model, key):
        return self.rec

    async def commit(self):
        pass


def run(rec, fetch_makes_file):
    told, queued = [], []

    async def fake_fetch(r):
        if fetch_makes_file:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            r.storage_path = path

    async def fake_tell(db, r):
        told.append(r.id)

    async def fake_enqueue(db, kind, payload):
        queued.append(kind)

    orig = (handlers._fetch_asterisk_local, handlers._tell_crm_the_recording_is_ready,
            handlers.queue.enqueue)
    handlers._fetch_asterisk_local = fake_fetch
    handlers._tell_crm_the_recording_is_ready = fake_tell
    handlers.queue.enqueue = fake_enqueue
    try:
        asyncio.run(handlers.handle_recording_fetch(
            Db(rec), {"recording_id": str(rec.id), "provider": "asterisk"}))
    finally:
        (handlers._fetch_asterisk_local, handlers._tell_crm_the_recording_is_ready,
         handlers.queue.enqueue) = orig
    return told, queued


told, queued = run(Rec(None), fetch_makes_file=True)
check("asterisk recording moved into place -> the CRM is told", len(told) == 1)
check("and it is still queued for transcription", queued == ["transcribe"])

fd, existing = tempfile.mkstemp(suffix=".wav"); os.close(fd)
told, _ = run(Rec(existing), fetch_makes_file=False)
check("already on disk (a re-run) -> the CRM is told (the CRM merge is idempotent)", len(told) == 1)

told, _ = run(Rec(None), fetch_makes_file=False)
check("audio NOT on disk -> the CRM is not told (no player over a 404)", told == [])

if FAILS:
    print(f"\n{len(FAILS)} FAILED"); sys.exit(1)
print("\nALL AGENT-RECORDING CHECKS PASSED")
