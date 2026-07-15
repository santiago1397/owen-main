"""Recording playback (Phase 2).

Storage is local disk, so a "signed URL" is a short-lived HMAC token pointing at our
own streaming endpoint — the raw path is never exposed, and the <audio> element can hit
the stream URL without an Authorization header. When storage later moves to S3/R2, only
`play` changes (return a presigned object URL instead).
"""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.api.deps import current_user
from app.core.security import create_playback_token, verify_playback_token
from app.db import get_db
from app.models import Recording, User

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


@router.get("/{recording_id}/play")
async def play(
    recording_id: uuid.UUID,
    user: User = Depends(current_user),
    db=Depends(get_db),
) -> dict:
    rec = await db.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording not found")
    if not rec.storage_path:
        raise HTTPException(status.HTTP_409_CONFLICT, "recording not yet downloaded")
    token = create_playback_token(str(recording_id))
    return {"url": f"/api/recordings/stream?token={token}", "expires_in": 300}


@router.get("/stream")
async def stream(token: str = Query(...), db=Depends(get_db)) -> FileResponse:
    recording_id = verify_playback_token(token)
    if not recording_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid or expired token")
    rec = await db.get(Recording, uuid.UUID(recording_id))
    if rec is None or not rec.storage_path or not os.path.exists(rec.storage_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recording file missing")
    return FileResponse(rec.storage_path, media_type="audio/mpeg",
                        filename=f"{rec.provider_recording_sid}.mp3")
