import re
from datetime import timedelta

from app.providers.messaging import MockSmsProvider
from tests.conftest import API, PASSWORD, auth, login, make_firm


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_login_success_and_me(client, firm):
    r = await client.get(f"{API}/auth/me", headers=firm.h("lawyer"))
    assert r.status_code == 200
    assert r.json()["role"] == "lawyer"
    assert r.json()["tenant_id"] == firm.tenant_id


async def test_login_wrong_password_is_401_and_audited(client, firm, super_token):
    r = await client.post(f"{API}/auth/login", json={"email": "lawyer@alpha.in", "password": "nope"})
    assert r.status_code == 401
    logs = (await client.get(f"{API}/admin/audit-logs?action=auth.login_failed", headers=auth(super_token))).json()
    assert logs["total"] == 1


async def test_unauthenticated_and_bad_token(client):
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=auth("garbage"))).status_code == 401


async def test_refresh_token_flow_and_type_check(client, firm):
    r = await client.post(f"{API}/auth/login", json={"email": "lawyer@alpha.in", "password": PASSWORD})
    tokens = r.json()
    r2 = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r2.status_code == 200 and r2.json()["access_token"]
    # an access token must not be accepted as a refresh token, and vice versa
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=auth(tokens["refresh_token"]))).status_code == 401


async def test_change_password(client, firm):
    h = firm.h("lawyer")
    bad = await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": "x", "new_password": "NewPassw0rd!"})
    assert bad.status_code == 400
    ok = await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": PASSWORD, "new_password": "NewPassw0rd!"})
    assert ok.status_code == 204
    await login(client, "lawyer@alpha.in", "NewPassw0rd!")


async def test_preferences_language_and_consent(client, firm):
    h = firm.h("client")
    r = await client.patch(f"{API}/auth/me", headers=h, json={"language": "te", "notify_call": True, "call_consent": True})
    assert r.status_code == 200
    body = r.json()
    assert body["language"] == "te" and body["notify_call"] and body["call_consent_at"]
    r = await client.patch(f"{API}/auth/me", headers=h, json={"call_consent": False})
    assert r.json()["call_consent_at"] is None
    assert (await client.patch(f"{API}/auth/me", headers=h, json={"language": "xx"})).status_code == 422


async def test_otp_login_flow(client, firm):
    phone = firm.client2["phone"]  # client2 has no email/password -> phone OTP only
    r = await client.post(f"{API}/auth/otp/request", json={"phone": phone})
    assert r.status_code == 202
    code = re.search(r"\b(\d{6})\b", MockSmsProvider.sent[-1]["body"]).group(1)
    # immediate second request is rate limited
    assert (await client.post(f"{API}/auth/otp/request", json={"phone": phone})).status_code == 429
    wrong = "000000" if code != "000000" else "111111"
    assert (await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": wrong})).status_code == 401
    r = await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "client"
    # code is single-use
    assert (await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": code})).status_code == 401


async def test_otp_unknown_phone_does_not_leak(client, firm):
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "+919999999999"})
    assert r.status_code == 202
    assert MockSmsProvider.sent == []


async def test_otp_attempt_limit(client, firm):
    phone = firm.client2["phone"]
    await client.post(f"{API}/auth/otp/request", json={"phone": phone})
    code = re.search(r"\b(\d{6})\b", MockSmsProvider.sent[-1]["body"]).group(1)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": wrong})
    # even the right code fails once attempts are exhausted
    assert (await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": code})).status_code == 401


# ------------------------------------------------------------------ super admin
async def test_tenant_crud_and_duplicates(client, super_token, firm):
    h = auth(super_token)
    dup = await client.post(
        f"{API}/admin/tenants",
        headers=h,
        json={"name": "Dup", "slug": "alpha", "admin_email": "x@x.in", "admin_name": "X", "admin_password": PASSWORD},
    )
    assert dup.status_code == 409
    lst = (await client.get(f"{API}/admin/tenants?q=alpha", headers=h)).json()
    assert lst["total"] == 1
    r = await client.patch(f"{API}/admin/tenants/{firm.tenant_id}", headers=h, json={"plan": "enterprise", "monthly_api_allowance": 20000})
    assert r.status_code == 200 and r.json()["plan"] == "enterprise"
    users = (await client.get(f"{API}/admin/tenants/{firm.tenant_id}/users", headers=h)).json()
    assert {u["role"] for u in users} == {"firm_admin", "lawyer", "junior_advocate", "client"}
    assert (await client.get(f"{API}/admin/tenants/00000000-0000-0000-0000-000000000000", headers=h)).status_code == 404


async def test_suspended_tenant_is_locked_out(client, super_token, firm):
    await client.patch(f"{API}/admin/tenants/{firm.tenant_id}", headers=auth(super_token), json={"status": "suspended"})
    assert (await client.get(f"{API}/auth/me", headers=firm.h("lawyer"))).status_code == 403
    r = await client.post(f"{API}/auth/login", json={"email": "lawyer@alpha.in", "password": PASSWORD})
    assert r.status_code == 403


async def test_only_super_admin_reaches_admin_routes(client, firm):
    for who in ("admin", "lawyer", "junior", "client"):
        assert (await client.get(f"{API}/admin/tenants", headers=firm.h(who))).status_code == 403


async def test_platform_overview_and_usage(client, super_token, firm):
    h = auth(super_token)
    await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "DLHC010001232024"})
    ov = (await client.get(f"{API}/admin/reports/overview", headers=h)).json()
    assert ov["tenants_by_status"] == {"active": 1}
    assert ov["users_by_role"]["lawyer"] == 1
    assert ov["integrations"][0]["provider"] == "mock" and ov["integrations"][0]["success_rate"] == 1.0
    usage = (await client.get(f"{API}/admin/usage", headers=h)).json()
    assert usage["court_api_requests"] == 1 and usage["estimated_total_inr"] == 0.5


async def test_tenant_validation(client, super_token):
    r = await client.post(
        f"{API}/admin/tenants",
        headers=auth(super_token),
        json={"name": "Bad", "slug": "Bad Slug!", "admin_email": "a@b.in", "admin_name": "A", "admin_password": PASSWORD},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ firm admin
async def test_firm_user_management_permissions(client, firm):
    # lawyers may add clients but not staff
    r = await client.post(
        f"{API}/firm/users", headers=firm.h("lawyer"), json={"full_name": "New Client", "role": "client", "phone": "+919833333333"}
    )
    assert r.status_code == 201
    r = await client.post(
        f"{API}/firm/users",
        headers=firm.h("lawyer"),
        json={"full_name": "Sneaky", "role": "lawyer", "phone": "+919855555555"},
    )
    assert r.status_code == 403
    # juniors and clients cannot add users
    for who in ("junior", "client"):
        r = await client.post(
            f"{API}/firm/users", headers=firm.h(who), json={"full_name": "X Y", "role": "client", "phone": "+919844444444"}
        )
        assert r.status_code == 403
    # everyone needs a mobile number (sign-in is mobile + OTP)
    assert (
        await client.post(f"{API}/firm/users", headers=firm.h("admin"), json={"full_name": "No Phone", "role": "lawyer", "email": "n@a.in"})
    ).status_code == 422
    # a lawyer with only a mobile number is valid
    assert (
        await client.post(
            f"{API}/firm/users", headers=firm.h("admin"), json={"full_name": "Phone Only", "role": "lawyer", "phone": "9866666666"}
        )
    ).status_code == 201
    # mobile numbers are unique across the platform
    assert (
        await client.post(
            f"{API}/firm/users", headers=firm.h("admin"), json={"full_name": "Dup Phone", "role": "client", "phone": firm.lawyer["phone"]}
        )
    ).status_code == 409
    assert (
        await client.post(f"{API}/firm/users", headers=firm.h("admin"), json={"full_name": "No Contact", "role": "client"})
    ).status_code == 422
    # duplicate email
    r = await client.post(
        f"{API}/firm/users",
        headers=firm.h("admin"),
        json={"full_name": "Dup", "role": "lawyer", "email": "lawyer@alpha.in", "phone": "9877777777"},
    )
    assert r.status_code == 409


async def test_firm_user_listing_and_updates(client, firm):
    lst = (await client.get(f"{API}/firm/users?role=client", headers=firm.h("admin"))).json()
    assert lst["total"] == 2
    # juniors don't see clients
    jl = (await client.get(f"{API}/firm/users", headers=firm.h("junior"))).json()
    assert all(u["role"] != "client" for u in jl["items"])
    r = await client.patch(f"{API}/firm/users/{firm.junior['id']}", headers=firm.h("admin"), json={"role": "lawyer"})
    assert r.status_code == 200 and r.json()["role"] == "lawyer"
    # lawyer can't edit staff or change roles
    assert (
        await client.patch(f"{API}/firm/users/{firm.admin['id']}", headers=firm.h("lawyer"), json={"full_name": "X"})
    ).status_code == 403
    # admin can't deactivate self
    assert (
        await client.patch(f"{API}/firm/users/{firm.admin['id']}", headers=firm.h("admin"), json={"is_active": False})
    ).status_code == 400
    # deactivated user loses access
    await client.patch(f"{API}/firm/users/{firm.client['id']}", headers=firm.h("admin"), json={"is_active": False})
    assert (await client.get(f"{API}/auth/me", headers=firm.h("client"))).status_code == 401


async def test_firm_profile_settings_merge(client, firm):
    r = await client.patch(f"{API}/firm", headers=firm.h("admin"), json={"settings": {"auto_hearing_reminders": False}})
    assert r.json()["settings"] == {"auto_hearing_reminders": False}
    r = await client.patch(f"{API}/firm", headers=firm.h("admin"), json={"settings": {"auto_reminder_channels": ["in_app", "sms"]}})
    assert r.json()["settings"] == {"auto_hearing_reminders": False, "auto_reminder_channels": ["in_app", "sms"]}
    assert (await client.patch(f"{API}/firm", headers=firm.h("lawyer"), json={"name": "X"})).status_code == 403
    assert (await client.get(f"{API}/firm", headers=firm.h("junior"))).status_code == 200


async def test_firm_audit_log_is_tenant_scoped(client, firm, other_firm):
    logs = (await client.get(f"{API}/firm/audit-logs", headers=firm.h("admin"))).json()
    assert logs["total"] > 0
    assert all(item["tenant_id"] == firm.tenant_id for item in logs["items"])


async def test_cross_tenant_user_access_is_404(client, firm, other_firm):
    r = await client.get(f"{API}/firm/users/{other_firm.lawyer['id']}", headers=firm.h("admin"))
    assert r.status_code == 404
    r = await client.patch(f"{API}/firm/users/{other_firm.client['id']}", headers=firm.h("admin"), json={"is_active": False})
    assert r.status_code == 404


async def test_second_firm_fixture_sanity(client, super_token):
    f = await make_firm(client, super_token, "gamma", client_lang="hi")
    assert f.client["language"] == "hi"


# ------------------------------------------------------------------ self registration & OTP dev mode
async def test_register_firm_requires_phone_otp(client, super_token, db):
    phone = "98480 22222"
    # step 1: registration code (works for an unregistered number)
    r = await client.post(f"{API}/auth/otp/request", json={"phone": phone, "purpose": "register"})
    assert r.status_code == 202 and len(r.json()["dev_code"]) == 6
    code = r.json()["dev_code"]
    body = {"firm_name": "Reddy & Associates", "full_name": "Anil Reddy", "phone": phone, "email": "Anil@Reddy.in"}
    # wrong code is rejected and nothing is created
    assert (
        await client.post(f"{API}/auth/register-firm", json={**body, "code": "000000" if code != "000000" else "111111"})
    ).status_code == 401
    r = await client.post(f"{API}/auth/register-firm", json={**body, "code": code})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["user"]["role"] == "firm_admin" and data["user"]["phone"] == "+919848022222"
    assert data["user"]["sms_consent_at"]  # verified own number
    firm = (await client.get(f"{API}/firm", headers=auth(data["access_token"]))).json()
    assert firm["name"] == "Reddy & Associates" and firm["slug"] == "reddy-associates"
    # the number is now taken: no second registration code
    again = await client.post(f"{API}/auth/otp/request", json={"phone": phone, "purpose": "register"})
    assert again.status_code == 409
    # and they sign in with a login OTP (after the 30-second resend window)
    from sqlalchemy import update

    from app.models import OtpCode

    await db.execute(update(OtpCode).values(created_at=OtpCode.created_at - timedelta(minutes=1)))
    await db.commit()
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "9848022222"})
    v = await client.post(f"{API}/auth/otp/verify", json={"phone": "9848022222", "code": r.json()["dev_code"]})
    assert v.status_code == 200 and v.json()["user"]["email"] == "anil@reddy.in"


async def test_register_firm_code_is_single_use(client):
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "9777700001", "purpose": "register"})
    code = r.json()["dev_code"]
    ok = await client.post(
        f"{API}/auth/register-firm", json={"firm_name": "One", "full_name": "First User", "phone": "9777700001", "code": code}
    )
    assert ok.status_code == 201
    dup = await client.post(
        f"{API}/auth/register-firm", json={"firm_name": "Two", "full_name": "Second User", "phone": "9777700001", "code": code}
    )
    assert dup.status_code == 409


async def test_password_login_disabled_by_default(client, firm, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PASSWORD_LOGIN_ENABLED", False)
    r = await client.post(f"{API}/auth/login", json={"email": "lawyer@alpha.in", "password": PASSWORD})
    assert r.status_code == 403 and "mobile" in r.json()["detail"]
    cfg = (await client.get(f"{API}/auth/config")).json()
    assert cfg["password_login"] is False and cfg["otp_login"] is True


async def test_profile_phone_must_stay_unique(client, firm):
    r = await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"phone": firm.client["phone"]})
    assert r.status_code == 409


async def test_register_firm_validation(client):
    r = await client.post(f"{API}/auth/register-firm", json={"firm_name": "X", "full_name": "A", "phone": "12"})
    assert r.status_code == 422


async def test_otp_dev_mode_returns_code_and_accepts_local_number_format(client, firm):
    # client2 was created as +91XXXXXXXXXX; user types it without country code, with a space
    local = firm.client2["phone"][-10:]
    r = await client.post(f"{API}/auth/otp/request", json={"phone": local[:5] + " " + local[5:]})
    assert r.status_code == 202 and len(r.json()["dev_code"]) == 6
    v = await client.post(f"{API}/auth/otp/verify", json={"phone": local, "code": r.json()["dev_code"]})
    assert v.status_code == 200 and v.json()["user"]["role"] == "client"


async def test_otp_dev_hint_for_unknown_number(client, firm):
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "9000000000"})
    assert r.status_code == 202 and "dev_hint" in r.json() and "dev_code" not in r.json()
