"""Exercise the real recording_fetch handler: HTTP download -> atomic file write ->
DB update -> idempotent re-run. Serves a fake mp3 from a local HTTP server.
"""

import asyncio
import http.server
import os
import socketserver
import threading
import uuid

from sqlalchemy import delete, select

from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, Recording
from app.services.recordings import _ensure_call
from app.services.ingestion import _get_or_create_provider
from app.workers.handlers import handle_recording_fetch

CALL_SID = "CA_dl_test"
REC_SID = "RE_dl_test"
AUDIO = b"ID3\x03\x00real-download-bytes-xyz"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"handler download failed at: {name}")


def serve(directory: str) -> tuple[socketserver.TCPServer, int]:
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


async def main():
    srv_dir = os.path.join(settings.RECORDINGS_DIR, "_srv")
    os.makedirs(srv_dir, exist_ok=True)
    with open(os.path.join(srv_dir, "audio.mp3"), "wb") as fh:
        fh.write(AUDIO)
    httpd, port = serve(srv_dir)

    # clean
    async with SessionLocal() as db:
        await db.execute(delete(Recording).where(Recording.provider_recording_sid == REC_SID))
        c = (await db.execute(select(Call).where(Call.provider_call_sid == CALL_SID))).scalar_one_or_none()
        if c:
            await db.execute(delete(Call).where(Call.id == c.id))
        await db.commit()

    async with SessionLocal() as db:
        prov = await _get_or_create_provider(db, "twilio")
        call = await _ensure_call(db, prov.id, CALL_SID)
        db.add(Recording(call_id=call.id, provider_recording_sid=REC_SID,
                         provider_url=f"http://127.0.0.1:{port}/audio.mp3", status="completed"))
        await db.commit()
        rec = (await db.execute(select(Recording).where(Recording.provider_recording_sid == REC_SID))).scalar_one()
        rec_id = rec.id

    print("real download:")
    async with SessionLocal() as db:
        await handle_recording_fetch(db, {"provider": "twilio", "recording_id": str(rec_id),
                                          "recording_sid": REC_SID})
    async with SessionLocal() as db:
        rec = await db.get(Recording, rec_id)
        check("storage_path set", bool(rec.storage_path))
        check("file on disk with correct bytes",
              os.path.exists(rec.storage_path) and open(rec.storage_path, "rb").read() == AUDIO)
        check("downloaded_at stamped", rec.downloaded_at is not None)
        check("no leftover .part temp file", not os.path.exists(rec.storage_path + ".part"))
        stored_path = rec.storage_path

    print("idempotent re-run:")
    async with SessionLocal() as db:
        # second run should skip (file exists), not error
        await handle_recording_fetch(db, {"provider": "twilio", "recording_id": str(rec_id),
                                          "recording_sid": REC_SID})
    check("file still present after re-run", os.path.exists(stored_path))

    # cleanup
    httpd.shutdown()
    os.remove(stored_path)
    os.remove(os.path.join(srv_dir, "audio.mp3"))
    async with SessionLocal() as db:
        await db.execute(delete(Recording).where(Recording.provider_recording_sid == REC_SID))
        c = (await db.execute(select(Call).where(Call.provider_call_sid == CALL_SID))).scalar_one_or_none()
        if c:
            await db.execute(delete(Call).where(Call.id == c.id))
        await db.commit()
    print("\nHANDLER DOWNLOAD CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
