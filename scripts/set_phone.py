"""Set (or change) the mobile number a user signs in with.

Usage: python -m scripts.set_phone admin@courtlife.in 9812345678
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import User
from app.schemas import normalize_phone


async def main(email: str, phone: str) -> None:
    phone = normalize_phone(phone)
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            print(f"No user with email {email}")
            return
        other = (await db.execute(select(User).where(User.phone == phone, User.id != user.id))).scalar_one_or_none()
        if other:
            print(f"{phone} already belongs to {other.email or other.full_name}")
            return
        if user.phone == phone:
            print(f"{email} already signs in with {phone}")
            return
        user.phone = phone
        await db.commit()
        print(f"{email} now signs in with {phone}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
