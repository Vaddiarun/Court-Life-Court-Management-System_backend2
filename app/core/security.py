import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _token(sub: uuid.UUID, kind: str, delta: timedelta, extra: dict | None = None) -> str:
    now = datetime.now(UTC)
    payload = {"sub": str(sub), "type": kind, "iat": now, "exp": now + delta, "jti": secrets.token_hex(8)}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID, role: str, tenant_id: uuid.UUID | None) -> str:
    return _token(
        user_id,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
        {"role": role, "tid": str(tenant_id) if tenant_id else None},
    )


def create_refresh_token(user_id: uuid.UUID) -> str:
    return _token(user_id, "refresh", timedelta(days=settings.REFRESH_TOKEN_DAYS))


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def generate_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_otp(code: str) -> str:
    return hashlib.sha256((code + settings.JWT_SECRET).encode()).hexdigest()
