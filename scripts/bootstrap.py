"""Production first-run: make sure a Super Admin exists who can sign in with OTP.

Reads SUPERADMIN_EMAIL, SUPERADMIN_NAME and SUPERADMIN_PHONE from the environment.
Safe to run on every deploy: it only creates the user or sets the phone when missing.
"""

import asyncio
import os
import secrets

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import User
from app.models.enums import Role
from app.schemas import normalize_phone


async def main() -> None:
    email = os.environ.get("SUPERADMIN_EMAIL", "").strip().lower()
    phone_raw = os.environ.get("SUPERADMIN_PHONE", "").strip()
    if not email or not phone_raw:
        print("bootstrap: SUPERADMIN_EMAIL / SUPERADMIN_PHONE not set - skipping")
        return
    phone = normalize_phone(phone_raw)
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            taken = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
            db.add(
                User(
                    email=email,
                    full_name=os.environ.get("SUPERADMIN_NAME", "Platform Admin"),
                    phone=None if taken else phone,
                    hashed_password=hash_password(secrets.token_urlsafe(24)),  # OTP-only sign-in
                    role=Role.SUPER_ADMIN,
                )
            )
            print(f"bootstrap: super admin {email} created")
        elif not user.phone:
            user.phone = phone
            print(f"bootstrap: {email} now signs in with {phone}")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
