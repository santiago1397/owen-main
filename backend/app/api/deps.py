from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# Calls at or under this many seconds are misdials / instant hang-ups (junk), not real
# leads. They are hidden from the calls list and excluded from dashboard stats by default;
# callers can opt back in per request via `include_short=true`.
SHORT_CALL_MAX_DURATION_SECONDS = 1

# Dashboard "likely junk" heuristic (distinct from the LLM spam verdict): a call is junk
# if it lasted this many seconds or fewer, OR it never connected (see JUNK_STATUSES).
# NULL durations / NULL statuses are treated as NOT junk (call may still be in flight).
JUNK_CALL_MAX_DURATION_SECONDS = 3
JUNK_STATUSES = ("failed", "busy", "no-answer", "canceled")


async def current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    user = (
        await db.execute(select(User).where(User.email == payload.get("sub")))
    ).scalar_one_or_none()
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "inactive or unknown user")
    return user
