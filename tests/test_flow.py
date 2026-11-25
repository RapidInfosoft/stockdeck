"""End-to-end flow with the mock recognizer:
catalog -> session -> photograph floor bay -> review/fix -> confirm (blueprint) ->
back room -> report totals -> rescan compares against blueprint."""


def _capture(client, sid, photo, zone, aisle, bay):
    r = client.post(
        f"/api/sessions/{sid}/captures",
        files={"image": ("bay.jpg", photo, "image/jpeg")},
        data={"zone": zone, "aisle": aisle, "bay": bay},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _slot(cap, text):
    return next(s for s in cap["slots"] if text in s["label"])


def _sku(client, q, size):
    return next(s for s in client.get("/api/catalog", params={"q": q}).json() if s["size"] == size)


def test_full_count_flow(client, photo):
    assert client.post("/api/catalog/load-sample").json()["added"] == 44
    sid = client.post("/api/sessions", json={"name": "Test count"}).json()["id"]

    # --- sales floor bay
    cap = _capture(client, sid, photo, "floor", "3", "2")
    assert cap["code"] == "F-3-2"
    assert cap["shelves"] == [1, 2, 3, 4]
    assert len(cap["slots"]) == 13
    # left-to-right positions per shelf
    assert [s["position"] for s in cap["slots"] if s["shelf"] == 1] == [1, 2, 3, 4]

    jd = _slot(cap, "Old No. 7")
    assert jd["size_label"] == "750ml" and jd["facings"] == 6 and jd["depth"] == 3 and jd["units"] == 18

    unknown = [s for s in cap["slots"] if not s["sku_id"]]
    assert len(unknown) == 1 and unknown[0]["raw_brand"] == "Belvedere"

    # Confirm is blocked while a product is unknown
    assert client.post(f"/api/captures/{cap['id']}/confirm").status_code == 409

    # Store isn't carrying Belvedere in the catalog yet -> add it, then assign
    bel = client.post("/api/catalog", json={"brand": "Belvedere", "name": "Vodka", "size_ml": 750, "case_pack": 6}).json()
    cap = client.patch(f"/api/slots/{unknown[0]['id']}", json={"sku_id": bel["id"]}).json()

    # Fix depth on Tito's 750 (5 facings, 4 deep)
    titos = next(s for s in cap["slots"] if s["label"] == "Tito's Handmade Vodka" and s["size_label"] == "750ml")
    cap = client.patch(f"/api/slots/{titos['id']}", json={"depth": 4}).json()
    assert next(s for s in cap["slots"] if s["id"] == titos["id"])["units"] == 20

    cap = client.post(f"/api/captures/{cap['id']}/confirm").json()
    assert cap["status"] == "confirmed"
    bp = client.get(f"/api/locations/{cap['location_id']}/blueprint").json()
    assert len(bp) == 13

    # --- back room
    back = _capture(client, sid, photo, "backroom", "BR", "1")
    crown = _slot(back, "Crown Royal")
    assert crown["review"] == "needs_review"  # brand-only read, several Crown Royal products
    fine = _sku(client, "Fine De Luxe", "750ml")
    back = client.patch(f"/api/slots/{crown['id']}", json={"sku_id": fine["id"]}).json()
    jd_case = _slot(back, "Old No. 7")
    assert jd_case["kind"] == "case" and jd_case["cases"] == 2 and jd_case["partial_units"] == 7
    assert jd_case["units"] == 2 * 12 + 7

    # manual add (e.g. something hidden, found by scanning its barcode)
    hen = _sku(client, "000000100131", "750ml")
    back = client.post(f"/api/captures/{back['id']}/slots", json={"sku_id": hen["id"], "kind": "case", "shelf": 2, "cases": 2}).json()
    assert _slot(back, "Hennessy")["units"] == 24
    client.post(f"/api/captures/{back['id']}/confirm")

    # --- report
    rep = client.get(f"/api/sessions/{sid}/report").json()
    line = {(i["brand"], i["name"], i["size"]): i for i in rep["items"]}
    jd750 = line[("Jack Daniel's", "Old No. 7 Tennessee Whiskey", "750ml")]
    assert jd750["loose_floor"] == 18
    assert jd750["loose_backroom"] == 7
    assert jd750["cases"] == 2 and jd750["case_units"] == 24
    assert jd750["total_units"] == 18 + 7 + 24

    t750 = line[("Tito's", "Handmade Vodka", "750ml")]
    assert t750["loose_floor"] == 20 and t750["cases"] == 8 and t750["total_units"] == 20 + 96
    assert line[("Fireball", "Cinnamon Whisky", "50ml")]["total_units"] == 200
    # Patron Silver in back room was loose bottles, not cases
    assert line[("Patron", "Silver Tequila", "750ml")]["loose_backroom"] == 4

    titos = next(p for p in rep["products"] if p["brand"] == "Tito's")
    assert [s["size"] for s in titos["sizes"]] == ["750ml", "1L", "1.75L"]
    assert titos["total_units"] == sum(s["total_units"] for s in titos["sizes"])
    assert rep["totals"]["units"] == sum(i["total_units"] for i in rep["items"])
    assert rep["unidentified"] == []

    csv_text = client.get(f"/api/sessions/{sid}/report.csv").text
    assert csv_text.splitlines()[0].startswith("upc,brand,name,size")
    assert "Old No. 7 Tennessee Whiskey,750ml,18,7,2,12,24,49" in csv_text

    # --- rescan same bay in a later count: compared against blueprint
    sid2 = client.post("/api/sessions", json={"name": "Next week"}).json()["id"]
    again = _capture(client, sid2, photo, "floor", "3", "2")
    assert again["has_blueprint"]
    # depth for Tito's 750 is carried over from the confirmed blueprint
    t = next(s for s in again["slots"] if s["label"] == "Tito's Handmade Vodka" and s["size_label"] == "750ml")
    assert t["depth"] == 4
    # Belvedere was added to the catalog during the first count, so it now matches automatically
    assert _slot(again, "Belvedere")["sku_id"] == bel["id"]
    assert {d["status"] for d in again["diff"]} == {"same"}


def test_blueprint_diff_statuses():
    from backend.app.inventory import blueprint_diff

    bp = [
        {"sku_id": 1, "shelf": 1, "facings": 3, "brand": "A", "name": "One", "size_ml": 750},
        {"sku_id": 2, "shelf": 2, "facings": 4, "brand": "B", "name": "Two", "size_ml": 750},
        {"sku_id": 3, "shelf": 3, "facings": 2, "brand": "C", "name": "Three", "size_ml": 750},
        {"sku_id": 4, "shelf": 3, "facings": 2, "brand": "D", "name": "Four", "size_ml": 750},
    ]
    now = [
        {"id": 10, "sku_id": 1, "shelf": 1, "facings": 3, "label": "A One", "size_label": "750ml"},
        {"id": 11, "sku_id": 2, "shelf": 1, "facings": 4, "label": "B Two", "size_label": "750ml"},
        {"id": 12, "sku_id": 3, "shelf": 3, "facings": 1, "label": "C Three", "size_label": "750ml"},
        {"id": 13, "sku_id": 9, "shelf": 2, "facings": 2, "label": "Z New", "size_label": "750ml"},
    ]
    got = {d["label"]: d["status"] for d in blueprint_diff(bp, now)}
    assert got == {"A One 750ml": "same", "B Two 750ml": "moved", "C Three 750ml": "qty_change",
                   "D Four 750ml": "missing", "Z New 750ml": "new"}


def test_rescan_supersedes_in_same_session(client, photo):
    client.post("/api/catalog/load-sample")
    sid = client.post("/api/sessions", json={}).json()["id"]
    _capture(client, sid, photo, "floor", "1", "1")
    _capture(client, sid, photo, "floor", "1", "1")
    caps = client.get(f"/api/sessions/{sid}/captures").json()
    assert len(caps) == 1
    rep = client.get(f"/api/sessions/{sid}/report").json()
    line = next(i for i in rep["items"] if i["brand"] == "Jameson")
    assert line["loose_floor"] == 3  # counted once, not twice


def test_rejects_non_image(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/captures", files={"image": ("x.jpg", b"not an image", "image/jpeg")},
                    data={"zone": "floor", "aisle": "1", "bay": "1"})
    assert r.status_code == 400


def test_validation_on_slot_edit(client, photo):
    client.post("/api/catalog/load-sample")
    sid = client.post("/api/sessions", json={}).json()["id"]
    cap = _capture(client, sid, photo, "floor", "1", "1")
    sid0 = cap["slots"][0]["id"]
    assert client.patch(f"/api/slots/{sid0}", json={"depth": 0}).status_code == 400
    assert client.patch(f"/api/slots/{sid0}", json={"facings": -1}).status_code == 400
    assert client.patch(f"/api/slots/{sid0}", json={"sku_id": 99999}).status_code == 400
