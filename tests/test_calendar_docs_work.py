from datetime import date

from tests.conftest import API, add_case

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


# ------------------------------------------------------------------ calendar
async def test_calendar_views_and_sources(client, firm):
    case = await add_case(client, firm)
    nhd = date.fromisoformat(case["next_hearing_date"])
    r = await client.post(
        f"{API}/hearings",
        headers=firm.h("lawyer"),
        json={"case_id": case["id"], "hearing_date": nhd.isoformat(), "start_time": "14:30", "purpose": "Client briefing"},
    )
    assert r.status_code == 201 and r.json()["source"] == "manual" and r.json()["case_title"]
    day = (await client.get(f"{API}/hearings?view=day&on={nhd}", headers=firm.h("lawyer"))).json()
    assert {h["source"] for h in day} == {"official", "manual"}
    week = (await client.get(f"{API}/hearings?view=week&on={nhd}", headers=firm.h("lawyer"))).json()
    assert len(week) >= 2
    month = (await client.get(f"{API}/hearings?view=month&on=2024-03-15", headers=firm.h("lawyer"))).json()
    assert [h["hearing_date"] for h in month] == ["2024-03-01"]
    official_only = (await client.get(f"{API}/hearings?from=2000-01-01&to=2100-01-01&source=official", headers=firm.h("lawyer"))).json()
    assert all(h["source"] == "official" for h in official_only) and len(official_only) == 2
    assert (await client.get(f"{API}/hearings?view=year", headers=firm.h("lawyer"))).status_code == 422


async def test_official_hearings_are_read_only(client, firm):
    case = await add_case(client, firm)
    official = (await client.get(f"{API}/hearings/case/{case['id']}", headers=firm.h("lawyer"))).json()[0]
    assert (await client.patch(f"{API}/hearings/{official['id']}", headers=firm.h("lawyer"), json={"purpose": "x"})).status_code == 403
    assert (await client.delete(f"{API}/hearings/{official['id']}", headers=firm.h("lawyer"))).status_code == 403


async def test_manual_hearing_crud(client, firm):
    case = await add_case(client, firm)
    h = (
        await client.post(
            f"{API}/hearings", headers=firm.h("lawyer"), json={"case_id": case["id"], "hearing_date": "2027-01-10", "purpose": "Mediation"}
        )
    ).json()
    r = await client.patch(f"{API}/hearings/{h['id']}", headers=firm.h("lawyer"), json={"court_room": "Room 4", "start_time": "11:00"})
    assert r.json()["court_room"] == "Room 4"
    assert (await client.patch(f"{API}/hearings/{h['id']}", headers=firm.h("lawyer"), json={"start_time": "11am"})).status_code == 422
    assert (await client.delete(f"{API}/hearings/{h['id']}", headers=firm.h("lawyer"))).status_code == 204


async def test_calendar_role_scoping(client, firm, other_firm):
    c1 = await add_case(client, firm, clients=[firm.client["id"]])
    await add_case(client, firm, cnr="TSHY020004562023")
    rng = "from=2000-01-01&to=2100-01-01"
    assert {h["case_id"] for h in (await client.get(f"{API}/hearings?{rng}", headers=firm.h("client"))).json()} == {c1["id"]}
    assert (await client.get(f"{API}/hearings?{rng}", headers=firm.h("junior"))).json() == []
    assert (await client.get(f"{API}/hearings?{rng}", headers=other_firm.h("admin"))).json() == []
    assert (
        await client.post(f"{API}/hearings", headers=firm.h("client"), json={"case_id": c1["id"], "hearing_date": "2027-01-01"})
    ).status_code == 403
    lawyer_filtered = (await client.get(f"{API}/hearings?{rng}&lawyer_id={firm.lawyer['id']}", headers=firm.h("admin"))).json()
    assert len(lawyer_filtered) == 3


# ------------------------------------------------------------------ documents
async def _upload(client, firm, case_id, who="lawyer", shared=False, name="petition.pdf", data=PDF, ctype="application/pdf"):
    return await client.post(
        f"{API}/cases/{case_id}/documents",
        headers=firm.h(who),
        files={"file": (name, data, ctype)},
        data={"category": "petition", "shared_with_client": str(shared).lower(), "description": "Main petition"},
    )


async def test_upload_list_download(client, firm):
    case = await add_case(client, firm)
    r = await _upload(client, firm, case["id"], name="../../etc/passwd petition.pdf")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["size_bytes"] == len(PDF) and "/" not in doc["filename"] and doc["category"] == "petition"
    lst = (await client.get(f"{API}/cases/{case['id']}/documents", headers=firm.h("lawyer"))).json()
    assert len(lst) == 1
    dl = await client.get(f"{API}/documents/{doc['id']}/download", headers=firm.h("lawyer"))
    assert dl.status_code == 200 and dl.content == PDF and "attachment" in dl.headers["content-disposition"]


async def test_upload_rejections(client, firm):
    case = await add_case(client, firm)
    assert (await _upload(client, firm, case["id"], name="x.exe", ctype="application/x-msdownload")).status_code == 415
    assert (await _upload(client, firm, case["id"], data=b"")).status_code == 422
    assert (await _upload(client, firm, case["id"], who="client")).status_code == 403


async def test_client_document_visibility_and_share_notification(client, firm):
    case = await add_case(client, firm, clients=[firm.client["id"]])
    private = (await _upload(client, firm, case["id"])).json()
    shared = (await _upload(client, firm, case["id"], shared=True, name="order.pdf")).json()
    lst = (await client.get(f"{API}/cases/{case['id']}/documents", headers=firm.h("client"))).json()
    assert [d["id"] for d in lst] == [shared["id"]]
    assert (await client.get(f"{API}/documents/{private['id']}/download", headers=firm.h("client"))).status_code == 404
    assert (await client.get(f"{API}/documents/{shared['id']}/download", headers=firm.h("client"))).status_code == 200
    # sharing later notifies the client
    r = await client.patch(f"{API}/documents/{private['id']}", headers=firm.h("lawyer"), json={"shared_with_client": True})
    assert r.json()["shared_with_client"]
    notes = (await client.get(f"{API}/notifications", headers=firm.h("client"))).json()["items"]
    assert sum(n["kind"] == "document_shared" for n in notes) == 2


async def test_document_delete_rules_and_isolation(client, firm, other_firm):
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    lawyer_doc = (await _upload(client, firm, case["id"])).json()
    junior_doc = (await _upload(client, firm, case["id"], who="junior")).json()
    assert (await client.delete(f"{API}/documents/{lawyer_doc['id']}", headers=firm.h("junior"))).status_code == 403
    assert (await client.delete(f"{API}/documents/{junior_doc['id']}", headers=firm.h("junior"))).status_code == 204
    assert (await client.get(f"{API}/documents/{lawyer_doc['id']}", headers=other_firm.h("admin"))).status_code == 404
    assert (await client.delete(f"{API}/documents/{lawyer_doc['id']}", headers=firm.h("lawyer"))).status_code == 204
    assert (await client.get(f"{API}/documents/{lawyer_doc['id']}", headers=firm.h("lawyer"))).status_code == 404


# ------------------------------------------------------------------ tasks
async def test_task_lifecycle_and_permissions(client, firm):
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    r = await client.post(
        f"{API}/tasks",
        headers=firm.h("lawyer"),
        json={"title": "Draft rejoinder", "case_id": case["id"], "assigned_to_id": firm.junior["id"]},
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    # junior can move status, not reassign
    assert (await client.patch(f"{API}/tasks/{tid}", headers=firm.h("junior"), json={"status": "in_progress"})).status_code == 200
    assert (
        await client.patch(f"{API}/tasks/{tid}", headers=firm.h("junior"), json={"assigned_to_id": firm.lawyer["id"]})
    ).status_code == 403
    assert (await client.patch(f"{API}/tasks/{tid}", headers=firm.h("junior"), json={"status": "nope"})).status_code == 422
    assert (await client.get(f"{API}/tasks?mine=true", headers=firm.h("junior"))).json()["total"] == 1
    assert (await client.get(f"{API}/tasks?status=in_progress", headers=firm.h("admin"))).json()["total"] == 1
    # can't assign to clients; juniors can't assign to others; clients can't use tasks
    assert (
        await client.post(f"{API}/tasks", headers=firm.h("lawyer"), json={"title": "x task", "assigned_to_id": firm.client["id"]})
    ).status_code == 422
    assert (
        await client.post(f"{API}/tasks", headers=firm.h("junior"), json={"title": "x task", "assigned_to_id": firm.lawyer["id"]})
    ).status_code == 403
    assert (await client.get(f"{API}/tasks", headers=firm.h("client"))).status_code == 403
    assert (await client.delete(f"{API}/tasks/{tid}", headers=firm.h("junior"))).status_code == 403
    assert (await client.delete(f"{API}/tasks/{tid}", headers=firm.h("lawyer"))).status_code == 204


# ------------------------------------------------------------------ notes
async def test_note_visibility_levels(client, firm):
    case = await add_case(client, firm, clients=[firm.client["id"]])
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    for vis in ("internal", "team", "client"):
        r = await client.post(f"{API}/cases/{case['id']}/notes", headers=firm.h("lawyer"), json={"body": f"{vis} note", "visibility": vis})
        assert r.status_code == 201

    def bodies(resp):
        return sorted(n["body"] for n in resp.json())

    assert bodies(await client.get(f"{API}/cases/{case['id']}/notes", headers=firm.h("lawyer"))) == [
        "client note",
        "internal note",
        "team note",
    ]
    assert bodies(await client.get(f"{API}/cases/{case['id']}/notes", headers=firm.h("junior"))) == ["client note", "team note"]
    assert bodies(await client.get(f"{API}/cases/{case['id']}/notes", headers=firm.h("client"))) == ["client note"]
    # junior can't write internal notes; clients can't write notes
    assert (
        await client.post(f"{API}/cases/{case['id']}/notes", headers=firm.h("junior"), json={"body": "x", "visibility": "internal"})
    ).status_code == 403
    assert (
        await client.post(f"{API}/cases/{case['id']}/notes", headers=firm.h("client"), json={"body": "x", "visibility": "client"})
    ).status_code == 403


async def test_note_edit_rules(client, firm):
    case = await add_case(client, firm)
    note = (await client.post(f"{API}/cases/{case['id']}/notes", headers=firm.h("lawyer"), json={"body": "draft"})).json()
    assert (await client.patch(f"{API}/notes/{note['id']}", headers=firm.h("lawyer"), json={"body": "final"})).json()["body"] == "final"
    # another lawyer-level user who is not the author: firm admin is allowed
    assert (await client.patch(f"{API}/notes/{note['id']}", headers=firm.h("admin"), json={"visibility": "team"})).status_code == 200
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    assert (await client.patch(f"{API}/notes/{note['id']}", headers=firm.h("junior"), json={"body": "hack"})).status_code == 403
    assert (await client.delete(f"{API}/notes/{note['id']}", headers=firm.h("lawyer"))).status_code == 204
