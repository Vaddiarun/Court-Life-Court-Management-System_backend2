"""Create the first Super Admin.

Usage: python -m scripts.create_superadmin admin@example.com "Platform Admin" 'StrongPassw0rd!'
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import User
from app.models.enums import Role


async def main(email: str, name: str, password: str) -> None:
    async with SessionLocal() as db:
        if (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none():
            print("User already exists")
            return
        db.add(User(email=email.lower(), full_name=name, hashed_password=hash_password(password), role=Role.SUPER_ADMIN))
        await db.commit()
        print(f"Super admin {email} created")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
