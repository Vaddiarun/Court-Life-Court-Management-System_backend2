from app.providers.court.mock import MockCourtProvider
from tests.conftest import API, add_case


async def test_lookup_validates_cnr(client, firm):
    for bad in ("123", "DLHC01000123202X", ""):
        r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": bad})
        assert r.status_code == 422


async def test_lookup_normalises_and_returns_record(client, firm):
    r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "dlhc01-000123-2024"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cnr"] == "DLHC010001232024"
    assert body["court_name"] == "High Court of Delhi"
    assert body["provider"] == "mock" and body["already_in_workspace"] is None
    assert len(body["hearings"]) == 2


async def test_lookup_not_found_is_404_and_still_metered(client, firm):
    r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "ZZZZ999999999999"})
    assert r.status_code == 404
    usage = (await client.get(f"{API}/usage", headers=firm.h("admin"))).json()
    assert usage["items"][0]["failures"] == 1


async def test_lookup_then_add_uses_cache_no_second_detail_call(client, firm):
    await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "DLHC010001232024"})
    case = await add_case(client, firm)
    details = [c for c in MockCourtProvider.calls if c[0] == "detail"]
    assert len(details) == 1  # lookup only; add reused the cached record
    assert case["source"] == "provider" and case["lead_lawyer_id"] == firm.lawyer["id"]
    assert case["last_synced_at"] and case["next_sync_at"]
    # the lookup now reports it's already in the workspace
    r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "DLHC010001232024"})
    assert r.json()["already_in_workspace"] == case["id"]


async def test_add_duplicate_cnr_is_409(client, firm):
    case = await add_case(client, firm)
    r = await client.post(f"{API}/cases/from-cnr", headers=firm.h("lawyer"), json={"cnr": "DLHC010001232024"})
    assert r.status_code == 409 and r.json()["detail"]["case_id"] == case["id"]


async def test_same_cnr_allowed_in_different_firms(client, firm, other_firm):
    a = await add_case(client, firm)
    b = await add_case(client, other_firm)
    assert a["id"] != b["id"]


async def test_add_case_creates_official_hearings_and_auto_reminder(client, firm):
    case = await add_case(client, firm, clients=[firm.client["id"]])
    hearings = (await client.get(f"{API}/hearings/case/{case['id']}", headers=firm.h("lawyer"))).json()
    assert len(hearings) == 2 and all(h["source"] == "official" for h in hearings)
    rem = (await client.get(f"{API}/reminders?case_id={case['id']}", headers=firm.h("lawyer"))).json()
    assert rem["total"] == 1 and rem["items"][0]["auto_generated"]
    assert firm.client["id"] in rem["items"][0]["recipient_ids"]


async def test_manual_case_and_duplicate_detection(client, firm):
    body = {"title": "Sharma vs Sharma", "case_number": "O.S. 77/2025", "court_name": "Family Court, Pune"}
    r = await client.post(f"{API}/cases", headers=firm.h("lawyer"), json=body)
    assert r.status_code == 201 and r.json()["source"] == "manual" and r.json()["sync_enabled"] is False
    dup = await client.post(f"{API}/cases", headers=firm.h("lawyer"), json={**body, "case_number": "os 77 / 2025"})
    assert dup.status_code == 409 and len(dup.json()["detail"]["case_ids"]) == 1
    forced = await client.post(f"{API}/cases", headers=firm.h("lawyer"), json={**body, "force": True})
    assert forced.status_code == 201


async def test_manual_case_with_cnr_enables_sync(client, firm):
    r = await client.post(
        f"{API}/cases",
        headers=firm.h("admin"),
        json={"title": "Manual with CNR", "cnr": "TSHY020004562023", "sync_enabled": True, "lead_lawyer_id": firm.lawyer["id"]},
    )
    assert r.status_code == 201
    assert r.json()["sync_enabled"] is True and r.json()["next_sync_at"]
    bad_lead = await client.post(f"{API}/cases", headers=firm.h("admin"), json={"title": "X case", "lead_lawyer_id": firm.client["id"]})
    assert bad_lead.status_code == 422


async def test_juniors_and_clients_cannot_create_cases(client, firm):
    for who in ("junior", "client"):
        assert (await client.post(f"{API}/cases", headers=firm.h(who), json={"title": "Nope case"})).status_code == 403
        assert (await client.post(f"{API}/cases/lookup", headers=firm.h(who), json={"cnr": "DLHC010001232024"})).status_code == 403


async def test_role_scoped_visibility(client, firm):
    c1 = await add_case(client, firm, clients=[firm.client["id"]])
    c2 = await add_case(client, firm, cnr="TSHY020004562023")
    # managers see all
    assert (await client.get(f"{API}/cases", headers=firm.h("admin"))).json()["total"] == 2
    # junior sees nothing until assigned
    assert (await client.get(f"{API}/cases", headers=firm.h("junior"))).json()["total"] == 0
    r = await client.post(f"{API}/cases/{c2['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    assert r.status_code == 200 and r.json()[0]["id"] == firm.junior["id"]
    jl = (await client.get(f"{API}/cases", headers=firm.h("junior"))).json()
    assert [c["id"] for c in jl["items"]] == [c2["id"]]
    # client sees only granted case, with restricted fields
    cl = (await client.get(f"{API}/cases", headers=firm.h("client"))).json()
    assert [c["id"] for c in cl["items"]] == [c1["id"]]
    assert "last_sync_error" not in cl["items"][0] and "lead_lawyer_id" not in cl["items"][0]
    assert (await client.get(f"{API}/cases/{c2['id']}", headers=firm.h("client"))).status_code == 404
    detail = (await client.get(f"{API}/cases/{c1['id']}", headers=firm.h("client"))).json()
    assert "sync_enabled" not in detail


async def test_cross_tenant_case_isolation(client, firm, other_firm):
    case = await add_case(client, firm)
    for who in ("admin", "lawyer"):
        assert (await client.get(f"{API}/cases/{case['id']}", headers=other_firm.h(who))).status_code == 404
        assert (await client.patch(f"{API}/cases/{case['id']}", headers=other_firm.h(who), json={"title": "Hacked"})).status_code == 404
    assert (await client.get(f"{API}/cases", headers=other_firm.h("admin"))).json()["total"] == 0
    # can't grant another firm's client
    r = await client.post(f"{API}/cases/{case['id']}/clients", headers=firm.h("lawyer"), json={"user_ids": [other_firm.client["id"]]})
    assert r.status_code == 404


async def test_list_filters_and_sorting(client, firm):
    await add_case(client, firm)
    await add_case(client, firm, cnr="TSHY020004562023")
    await add_case(client, firm, cnr="MHMU030007892022")
    h = firm.h("lawyer")
    assert (await client.get(f"{API}/cases?q=lakshmi", headers=h)).json()["total"] == 1
    assert (await client.get(f"{API}/cases?disposed=true", headers=h)).json()["total"] == 1
    assert (await client.get(f"{API}/cases?status=pending", headers=h)).json()["total"] == 2
    assert (await client.get(f"{API}/cases?court=Delhi", headers=h)).json()["total"] == 1
    items = (await client.get(f"{API}/cases?sort=next_hearing", headers=h)).json()["items"]
    assert items[0]["cnr"] == "DLHC010001232024"  # soonest hearing first, disposed (no date) last
    assert items[-1]["cnr"] == "MHMU030007892022"
    page = (await client.get(f"{API}/cases?limit=1&offset=1", headers=h)).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    by_lawyer = (await client.get(f"{API}/cases?lawyer_id={firm.lawyer['id']}", headers=h)).json()
    assert by_lawyer["total"] == 3


async def test_update_case_records_manual_events(client, firm):
    case = await add_case(client, firm)
    r = await client.patch(
        f"{API}/cases/{case['id']}", headers=firm.h("lawyer"), json={"stage": "Final Arguments", "description": "Key matter"}
    )
    assert r.status_code == 200 and r.json()["stage"] == "Final Arguments"
    events = (await client.get(f"{API}/cases/{case['id']}/events", headers=firm.h("lawyer"))).json()
    assert events[0]["field"] == "stage" and events[0]["source"] == "manual"
    assert (await client.patch(f"{API}/cases/{case['id']}", headers=firm.h("junior"), json={"title": "x"})).status_code == 403


async def test_sync_requires_cnr(client, firm):
    r = await client.post(f"{API}/cases", headers=firm.h("lawyer"), json={"title": "No CNR case"})
    cid = r.json()["id"]
    assert (await client.patch(f"{API}/cases/{cid}", headers=firm.h("lawyer"), json={"sync_enabled": True})).status_code == 422
    assert (await client.post(f"{API}/cases/{cid}/sync", headers=firm.h("lawyer"))).status_code == 422


async def test_delete_case_permissions(client, firm):
    case = await add_case(client, firm, who="admin")  # admin-created: no lead lawyer
    assert (await client.delete(f"{API}/cases/{case['id']}", headers=firm.h("lawyer"))).status_code == 403
    assert (await client.delete(f"{API}/cases/{case['id']}", headers=firm.h("admin"))).status_code == 204
    assert (await client.get(f"{API}/cases/{case['id']}", headers=firm.h("admin"))).status_code == 404


async def test_client_access_grant_revoke_and_notification(client, firm):
    case = await add_case(client, firm)
    r = await client.post(f"{API}/cases/{case['id']}/clients", headers=firm.h("lawyer"), json={"user_ids": [firm.client["id"]]})
    assert r.status_code == 200 and len(r.json()) == 1
    notes = (await client.get(f"{API}/notifications", headers=firm.h("client"))).json()
    assert any(n["kind"] == "case_shared" for n in notes["items"])
    # granting a staff member as client is rejected
    assert (
        await client.post(f"{API}/cases/{case['id']}/clients", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    ).status_code == 422
    assert (
        await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.client["id"]]})
    ).status_code == 422
    assert (await client.delete(f"{API}/cases/{case['id']}/clients/{firm.client['id']}", headers=firm.h("lawyer"))).status_code == 204
    assert (await client.get(f"{API}/cases/{case['id']}", headers=firm.h("client"))).status_code == 404


async def test_unassign_staff(client, firm):
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    assert (await client.delete(f"{API}/cases/{case['id']}/assignments/{firm.junior['id']}", headers=firm.h("lawyer"))).status_code == 204
    assert (await client.get(f"{API}/cases/{case['id']}", headers=firm.h("junior"))).status_code == 404


async def test_provider_search(client, firm):
    r = await client.post(f"{API}/cases/search", headers=firm.h("lawyer"), json={"query": "lakshmi"})
    assert r.status_code == 200 and r.json()["results"][0]["cnr"] == "TSHY020004562023"
    usage = (await client.get(f"{API}/usage", headers=firm.h("admin"))).json()
    assert usage["items"][0]["operation"] == "search" and usage["estimated_total_inr"] == 0.2


async def test_lookup_unindexed_cnr_requests_live_fetch_then_succeeds(client, firm, monkeypatch):
    """A brand-new CNR is not in the provider index: we ask for a live fetch and poll."""
    from dataclasses import replace

    from app.core.config import settings

    cnr = "APAN150001142026"
    seed = MockCourtProvider._store["DLHC010001232024"]
    real_refresh = MockCourtProvider.request_refresh

    async def refresh_then_index(self, c):
        await real_refresh(self, c)
        MockCourtProvider.upsert(replace(seed, cnr=c, title="Live fetched case"))

    monkeypatch.setattr(MockCourtProvider, "request_refresh", refresh_then_index)
    monkeypatch.setattr(settings, "LOOKUP_REFRESH_WAIT_SECONDS", 1)
    monkeypatch.setattr(settings, "LOOKUP_REFRESH_POLL_SECONDS", 0.01)
    r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": cnr})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Live fetched case"
    assert ("refresh", cnr) in MockCourtProvider.calls


async def test_lookup_unindexed_cnr_reports_pending(client, firm):
    r = await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "APAN150001142026"})
    assert r.status_code == 404
    assert "try again" in r.json()["detail"]
    assert ("refresh", "APAN150001142026") in MockCourtProvider.calls
