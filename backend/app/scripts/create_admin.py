"""Create/reset the first admin user.

Usage (inside the app container):
    python -m app.scripts.create_admin admin@example.com 'a-strong-password'
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.security import hash_password
from app.db import SessionLocal
from app.models import User


async def main(email: str, password: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            pg_insert(User)
            .values(email=email, password_hash=hash_password(password), role="admin")
            .on_conflict_do_update(
                index_elements=["email"],
                set_={"password_hash": hash_password(password), "active": True},
            )
        )
        await db.commit()
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        print(f"admin ready: {user.email}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m app.scripts.create_admin <email> <password>")
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
