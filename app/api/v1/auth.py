"""Authentication.

Primary sign-in is mobile number + OTP. Password sign-in exists only when
PASSWORD_LOGIN_ENABLED=true (off by default). Registering a law firm requires
verifying the admin's mobile number with an OTP first.
"""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, update

from app.api.deps import DB, CurrentUser, audit
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    hash_otp,
    hash_password,
    verify_password,
)
from app.i18n import t
from app.models import OtpCode, Tenant, User
from app.models.enums import Role
from app.providers.messaging import ChannelError, get_email, get_sms, get_voice
from app.providers.otp import OtpProviderError, get_remote_otp_provider
from app.schemas import (
    ChangePasswordIn,
    FirmRegisterIn,
    LoginIn,
    OtpRequestIn,
    OtpVerifyIn,
    PreferencesUpdate,
    RefreshIn,
    TokenOut,
    UserOut,
)
from app.services.usage import record_usage

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("courtlife.auth")
GENERIC_OTP_MSG = "If the number is registered, a code has been sent."


def _tokens(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_access_token(user.id, user.role, user.tenant_id),
        refresh_token=create_refresh_token(user.id),
        user=UserOut.model_validate(user),
    )


def _check_login_allowed(user: User | None) -> User:
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if user.tenant is not None and user.tenant.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Law firm account is suspended")
    return user


async def _user_by_phone(db, phone: str) -> User | None:
    return (await db.execute(select(User).where(User.phone == phone, User.is_active.is_(True)))).scalars().first()


async def phone_taken(db, phone: str | None, exclude_id: uuid.UUID | None = None) -> bool:
    if not phone:
        return False
    q = select(User.id).where(User.phone == phone)
    if exclude_id:
        q = q.where(User.id != exclude_id)
    return (await db.execute(q)).first() is not None


@router.get("/config")
async def auth_config():
    """Tells the UI which sign-in methods are enabled."""
    return {
        "password_login": settings.PASSWORD_LOGIN_ENABLED,
        "otp_login": True,
        "firm_registration": settings.ALLOW_FIRM_SELF_REGISTRATION,
        "otp_provider": settings.OTP_PROVIDER,
        "dev_otp_on_screen": settings.otp_dev_echo,
    }


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: DB, request: Request):
    if not settings.PASSWORD_LOGIN_ENABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password sign-in is disabled. Sign in with your mobile number and OTP.")
    user = (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        await audit(
            db, None, "auth.login_failed", "user", None, {"email": body.email.lower()}, request, tenant_id=user.tenant_id if user else None
        )
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    _check_login_allowed(user)
    user.last_login_at = datetime.now(UTC)
    await audit(db, user, "auth.login", "user", user.id, request=request)
    return _tokens(user)


# ------------------------------------------------------------------ OTP
async def _send_otp(db, phone: str, language: str, tenant_id) -> str | None:
    """Create + deliver an OTP for `phone`. Returns the code only for the local provider."""
    recent = (
        await db.execute(select(OtpCode).where(OtpCode.phone == phone, OtpCode.created_at > datetime.now(UTC) - timedelta(seconds=30)))
    ).first()
    if recent:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait 30 seconds before requesting another code")
    await db.execute(update(OtpCode).where(OtpCode.phone == phone, OtpCode.used.is_(False)).values(used=True))
    expires = datetime.now(UTC) + timedelta(minutes=settings.OTP_TTL_MINUTES)
    remote = get_remote_otp_provider()
    if remote is not None:
        try:
            sent = await remote.send(phone)
        except OtpProviderError as e:
            log.error("OTP send failed: %s", e)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send OTP. Please try again.") from None
        db.add(OtpCode(phone=phone, code_hash="-", provider=remote.name, provider_ref=sent.reference, expires_at=expires))
        record_usage(db, tenant_id, remote.name, "sms")
        return None
    code = generate_otp()
    db.add(OtpCode(phone=phone, code_hash=hash_otp(code), provider="local", expires_at=expires))
    try:
        await get_sms().send(phone, t("otp.sms", language, code=code, minutes=settings.OTP_TTL_MINUTES), template="otp", variables=[code])
        record_usage(db, tenant_id, get_sms().name, "sms")
    except ChannelError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send OTP") from None
    if settings.otp_dev_echo:
        log.warning("DEV OTP for %s: %s", phone, code)
    return code


async def _consume_otp(db, phone: str, code: str) -> None:
    """Validate the latest unused OTP for `phone`; raises 401 when wrong/expired."""
    otp = (
        await db.execute(
            select(OtpCode).where(OtpCode.phone == phone, OtpCode.used.is_(False)).order_by(OtpCode.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if otp is None or otp.expires_at < datetime.now(UTC) or otp.attempts >= settings.OTP_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code expired or invalid. Request a new one.")
    if otp.provider == "local":
        valid = otp.code_hash == hash_otp(code)
    else:
        remote = get_remote_otp_provider()
        valid = bool(remote and otp.provider_ref and await remote.verify(phone, code, otp.provider_ref))
    if not valid:
        otp.attempts += 1
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect code. Please check the SMS and try again.")
    otp.used = True


@router.post("/otp/request", status_code=202)
async def otp_request(body: OtpRequestIn, db: DB):
    """Send a sign-in code (purpose=login) or a registration code (purpose=register)."""
    if body.purpose == "register":
        if not settings.ALLOW_FIRM_SELF_REGISTRATION:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Self-registration is disabled.")
        if await phone_taken(db, body.phone):
            raise HTTPException(status.HTTP_409_CONFLICT, "This mobile number is already registered. Please sign in.")
        code = await _send_otp(db, body.phone, "en", None)
        out = {"detail": "A verification code has been sent to your mobile."}
    else:
        user = await _user_by_phone(db, body.phone)
        if user is None:
            out = {"detail": GENERIC_OTP_MSG}
            if settings.otp_dev_echo:  # development only: say why nothing arrives
                out["dev_hint"] = f"No active user has the mobile number {body.phone}. Register your firm or ask your admin to add you."
            return out
        code = await _send_otp(db, body.phone, user.language, user.tenant_id)
        out = {"detail": GENERIC_OTP_MSG}
    if code and settings.otp_dev_echo:
        out["dev_code"] = code  # mock SMS only — never in production
    return out


@router.post("/otp/verify", response_model=TokenOut)
async def otp_verify(body: OtpVerifyIn, db: DB, request: Request):
    await _consume_otp(db, body.phone, body.code)
    user = await _user_by_phone(db, body.phone)
    _check_login_allowed(user)
    user.last_login_at = datetime.now(UTC)
    await audit(db, user, "auth.login_otp", "user", user.id, request=request)
    return _tokens(user)


# ------------------------------------------------------------------ firm registration
async def _unique_slug(db, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "firm"
    base = base if len(base) >= 3 else f"{base}-firm"
    slug = base
    while (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).first():
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


@router.post("/register-firm", response_model=TokenOut, status_code=201)
async def register_firm(body: FirmRegisterIn, db: DB, request: Request):
    """Public sign-up. Step 1: POST /auth/otp/request {phone, purpose:"register"}.
    Step 2: this endpoint with the code -> creates the firm + Firm Admin and signs them in."""
    if not settings.ALLOW_FIRM_SELF_REGISTRATION:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Self-registration is disabled. Contact the platform administrator.")
    if await phone_taken(db, body.phone):
        raise HTTPException(status.HTTP_409_CONFLICT, "This mobile number is already registered. Please sign in.")
    email = body.email.lower() if body.email else None
    if email and (await db.execute(select(User.id).where(User.email == email))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")
    await _consume_otp(db, body.phone, body.code)
    tenant = Tenant(
        name=body.firm_name.strip(),
        slug=await _unique_slug(db, body.firm_name),
        plan=settings.SELF_REGISTRATION_PLAN,
        default_language=body.default_language,
        contact_email=email,
        contact_phone=body.phone,
        settings={},
    )
    db.add(tenant)
    await db.flush()
    now = datetime.now(UTC)
    user = User(
        tenant_id=tenant.id,
        email=email,
        phone=body.phone,
        full_name=body.full_name.strip(),
        role=Role.FIRM_ADMIN,
        language=body.default_language,
        bar_council_id=body.bar_council_id,
        last_login_at=now,
        notify_sms=True,
        sms_consent_at=now,  # verified their own number by OTP
    )
    db.add(user)
    await db.flush()
    await db.refresh(user, ["tenant"])
    await audit(db, user, "tenant.self_registered", "tenant", tenant.id, {"slug": tenant.slug}, request)
    return _tokens(user)


# ------------------------------------------------------------------ session & profile
@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: DB):
    try:
        payload = decode_token(body.refresh_token, "refresh")
        user = await db.get(User, uuid.UUID(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None
    return _tokens(_check_login_allowed(user))


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(body: PreferencesUpdate, user: CurrentUser, db: DB):
    data = body.model_dump(exclude_unset=True)
    if "phone" in data:
        if not data["phone"]:
            raise HTTPException(422, "A mobile number is required to sign in")
        if await phone_taken(db, data["phone"], exclude_id=user.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "This mobile number is already used by another account")
    now = datetime.now(UTC)
    if "sms_consent" in data:
        user.sms_consent_at = now if data.pop("sms_consent") else None
    if "call_consent" in data:
        user.call_consent_at = now if data.pop("call_consent") else None
    for k, v in data.items():
        setattr(user, k, v)
    await audit(db, user, "user.preferences_updated", "user", user.id, {"fields": sorted(body.model_dump(exclude_unset=True))})
    await db.flush()
    return user


@router.post("/me/test-call")
async def test_call(user: CurrentUser, db: DB):
    """Place a real reminder-style call to your own number right now (ignores the call window)."""
    if not user.phone:
        raise HTTPException(422, "Add your mobile number in your profile first")
    voice = get_voice()
    lang = user.language or "en"
    msg = t("call.test", lang, name=user.full_name)
    try:
        result = await voice.place_call(user.phone, msg, lang, f"test-{user.id}")
    except ChannelError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Call could not be placed: {e}") from None
    record_usage(db, user.tenant_id, voice.name, "call", units=1)
    await audit(db, user, "user.test_call", "user", user.id, {"provider": voice.name, "call_id": result.call_id})
    return {"provider": voice.name, "call_id": result.call_id, "status": result.status, "to": user.phone}


@router.post("/me/test-email")
async def test_email(user: CurrentUser, db: DB):
    """Send a test email to your own address with the configured email provider."""
    if not user.email:
        raise HTTPException(422, "Add your email address in your profile first")
    mailer = get_email()
    try:
        ref = await mailer.send(
            user.email,
            "Court Life: test email",
            f"Hello {user.full_name},\n\nThis is a test email from Court Life. "
            "Your hearing reminders by email will arrive like this.\n\nThank you.",
        )
    except ChannelError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Email could not be sent: {e}") from None
    record_usage(db, user.tenant_id, mailer.name, "email")
    await audit(db, user, "user.test_email", "user", user.id, {"provider": mailer.name})
    return {"provider": mailer.name, "to": user.email, "ref": ref}


@router.get("/me/channels")
async def channel_status(user: CurrentUser):
    """Which delivery channels are live on this server (for the profile page)."""
    return {
        "otp": settings.OTP_PROVIDER,
        "sms": settings.SMS_PROVIDER,
        "voice": settings.VOICE_PROVIDER,
        "email": settings.EMAIL_PROVIDER,
        "sms_templates": sorted(settings.FAST2SMS_TEMPLATES) if settings.SMS_PROVIDER == "fast2sms" else [],
        "public_base_url_set": bool(settings.PUBLIC_BASE_URL),
    }


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, user: CurrentUser, db: DB):
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.hashed_password = hash_password(body.new_password)
    await audit(db, user, "auth.password_changed", "user", user.id)
