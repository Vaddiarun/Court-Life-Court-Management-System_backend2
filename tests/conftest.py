import os
import tempfile

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/cms_test")
os.environ["COURT_PROVIDER"] = "mock"
os.environ["VOICE_PROVIDER"] = "mock"
os.environ["SMS_PROVIDER"] = "mock"
os.environ["EMAIL_PROVIDER"] = "mock"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_DIR"] = tempfile.mkdtemp(prefix="cms-test-storage-")
os.environ["JWT_SECRET"] = "test-secret-please-change-0123456789abcdef"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["PASSWORD_LOGIN_ENABLED"] = "true"  # fixtures sign in with passwords; OTP-only is tested explicitly
os.environ["RUN_SCHEDULER_IN_PROCESS"] = "false"
os.environ["OTP_PROVIDER"] = "local"
os.environ["LOOKUP_REFRESH_WAIT_SECONDS"] = "0"  # no live-fetch polling in tests

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models
from app.core.db import Base, get_db, make_engine
from app.core.security import hash_password
from app.main import app
from app.models import User
from app.providers.court import set_court_provider
from app.providers.court.mock import MockCourtProvider
from app.providers.messaging import reset_mocks

API = "/api/v1"
PASSWORD = "Passw0rd!123"


@pytest.fixture(scope="session")
def engine():
    eng = make_engine(os.environ["DATABASE_URL"], null_pool=True)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.run(_create())
    return eng


@pytest.fixture
def Session(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(engine):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    MockCourtProvider.reset()
    set_court_provider(MockCourtProvider())
    reset_mocks()
    yield
    set_court_provider(None)


@pytest.fixture
async def db(Session):
    async with Session() as s:
        yield s
        await s.commit()


@pytest.fixture
async def client(Session):
    async def _get_db():
        async with Session() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def login(client, email, password=PASSWORD) -> str:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@dataclass
class Firm:
    tenant_id: str
    admin: dict
    lawyer: dict
    junior: dict
    client: dict
    client2: dict
    tokens: dict = field(default_factory=dict)

    def h(self, who: str) -> dict:
        return auth(self.tokens[who])


def firm_phone(slug: str, n: int) -> str:
    """Unique, deterministic mobile per firm + person (phones are unique platform-wide)."""
    base = sum(ord(c) * (i + 1) for i, c in enumerate(slug)) % 10000
    return f"+9197{base:04d}{n:04d}"


async def make_firm(client, super_token: str, slug: str, client_lang: str = "en") -> Firm:
    r = await client.post(
        f"{API}/admin/tenants",
        headers=auth(super_token),
        json={
            "name": f"{slug.title()} Associates",
            "slug": slug,
            "admin_email": f"admin@{slug}.in",
            "admin_name": f"{slug} Admin",
            "admin_password": PASSWORD,
        },
    )
    assert r.status_code == 201, r.text
    tenant_id = r.json()["id"]
    admin_token = await login(client, f"admin@{slug}.in")
    users = {}
    for key, role, extra in [
        ("lawyer", "lawyer", {"email": f"lawyer@{slug}.in", "password": PASSWORD, "phone": firm_phone(slug, 1)}),
        ("junior", "junior_advocate", {"email": f"junior@{slug}.in", "password": PASSWORD, "phone": firm_phone(slug, 2)}),
        ("client", "client", {"email": f"client@{slug}.in", "password": PASSWORD, "phone": firm_phone(slug, 3), "language": client_lang}),
        ("client2", "client", {"phone": firm_phone(slug, 4)}),
    ]:
        r = await client.post(
            f"{API}/firm/users", headers=auth(admin_token), json={"full_name": f"{key.title()} {slug}", "role": role, **extra}
        )
        assert r.status_code == 201, r.text
        users[key] = r.json()
    me = (await client.get(f"{API}/auth/me", headers=auth(admin_token))).json()
    firm = Firm(tenant_id, me, users["lawyer"], users["junior"], users["client"], users["client2"])
    firm.tokens = {
        "admin": admin_token,
        "lawyer": await login(client, f"lawyer@{slug}.in"),
        "junior": await login(client, f"junior@{slug}.in"),
        "client": await login(client, f"client@{slug}.in"),
    }
    return firm


@pytest.fixture
async def super_token(client, db):
    db.add(User(email="root@platform.in", full_name="Root", hashed_password=hash_password(PASSWORD), role="super_admin"))
    await db.commit()
    return await login(client, "root@platform.in")


@pytest.fixture
async def firm(client, super_token) -> Firm:
    return await make_firm(client, super_token, "alpha")


@pytest.fixture
async def other_firm(client, super_token) -> Firm:
    return await make_firm(client, super_token, "beta")


async def add_case(client, firm: Firm, cnr="DLHC010001232024", who="lawyer", clients=()) -> dict:
    r = await client.post(f"{API}/cases/from-cnr", headers=firm.h(who), json={"cnr": cnr, "client_ids": list(clients)})
    assert r.status_code == 201, r.text
    return r.json()
