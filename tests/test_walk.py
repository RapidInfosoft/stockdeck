"""Walk-through flow (mock recognizer):
run 1 = map the aisle from overlapping video snapshots, run 2 = point-and-count using the saved map."""

import io

from PIL import Image


def frame(index: int) -> bytes:
    """The mock picks the walk frame / section from the top-left pixel (index * 40 + 10)."""
    img = Image.new("RGB", (640, 480), "white")
    img.putpixel((0, 0), (index * 40 + 10,) * 3)
    buf = io.BytesIO()
    img.save(buf, "PNG")  # lossless, so the marker pixel survives
    return buf.getvalue()


def _map(client, sid, seq, idx=None):
    r = client.post(f"/api/sessions/{sid}/walk/map", files={"image": ("f.png", frame(seq - 1 if idx is None else idx), "image/png")},
                    data={"zone": "floor", "aisle": "7", "seq": str(seq)})
    assert r.status_code == 200, r.text
    return r.json()


def _count(client, sid, idx):
    return client.post(f"/api/sessions/{sid}/walk/count", files={"image": ("c.png", frame(idx), "image/png")},
                       data={"zone": "floor", "aisle": "7"})


def _line(rep, brand, name, size="750ml"):
    return next(i for i in rep["items"] if i["brand"] == brand and i["name"] == name and i["size"] == size)


def test_map_run_then_count_run(client):
    client.post("/api/catalog/load-sample")
    sid = client.post("/api/sessions", json={"name": "Map run"}).json()["id"]

    # Uploads can finish out of order while walking: send snapshot 2 first.
    s2 = _map(client, sid, 2)
    s1 = _map(client, sid, 1)
    s3 = _map(client, sid, 3)
    assert [s1["code"], s2["code"], s3["code"]] == ["F-7-S01", "F-7-S02", "F-7-S03"]

    secs = client.get(f"/api/sessions/{sid}/walk/sections", params={"zone": "floor", "aisle": "7"}).json()
    assert [s["seq"] for s in secs] == [1, 2, 3]
    # Don Julio Blanco (S01 right edge / S02 left edge) and Hennessy (S02 right / S03 left) are overlaps.
    assert sum(s["duplicates"] for s in secs) == 2

    rep = client.get(f"/api/sessions/{sid}/report").json()
    assert _line(rep, "Don Julio", "Blanco Tequila")["loose_floor"] == 3      # max(3, 2), not 5
    assert _line(rep, "Hennessy", "V.S Cognac")["loose_floor"] == 3           # not 6
    # Captain Morgan 750 (S02 right) and 1.75L (S03 left) are different SKUs: both kept
    assert _line(rep, "Captain Morgan", "Original Spiced Rum")["loose_floor"] == 4
    assert _line(rep, "Captain Morgan", "Original Spiced Rum", "1.75L")["loose_floor"] == 2

    # Reference photos were cut out for each product group
    cap = client.get(f"/api/captures/{s1['id']}").json()
    patron = next(s for s in cap["slots"] if s["label"] == "Patron Silver Tequila")
    assert patron["has_crop"]
    crop = client.get(f"/api/slots/{patron['id']}/crop")
    assert crop.status_code == 200 and crop.content[:2] == b"\xff\xd8"
    refs = client.get(f"/api/skus/{patron['sku_id']}/references").json()
    assert refs and refs[0]["slot_id"] == patron["id"]

    # Count run before the map is confirmed -> clear error
    assert _count(client, sid, 0).status_code == 409

    # Patron Reposado matched below auto-accept? fix anything unidentified, then confirm the whole aisle
    r = client.post(f"/api/sessions/{sid}/walk/confirm", params={"zone": "floor", "aisle": "7"})
    assert r.status_code == 200, r.text
    assert r.json()["confirmed"] == 3

    # The duplicate is not part of the S02 blueprint (it belongs to S01)
    bp2 = client.get(f"/api/locations/{s2['location_id']}/blueprint").json()
    assert "Blanco Tequila" not in [b["name"] for b in bp2 if b["brand"] == "Don Julio"]

    # ---- run 2: point and count, in a new count session
    sid2 = client.post("/api/sessions", json={"name": "Weekly count"}).json()["id"]
    r = _count(client, sid2, 1)   # mock: photo shows section S02
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["code"] == "F-7-S02" and c["mode"] == "count"
    by_name = {s["label"]: s for s in c["slots"]}
    # first slot sold one facing, second sold out; SKUs come from the map, not re-identified
    first, second = sorted(bp2, key=lambda b: (b["shelf"], b["position"]))[:2]
    assert by_name[f"{first['brand']} {first['name']}"]["facings"] == first["facings"] - 1
    assert by_name[f"{second['brand']} {second['name']}"]["facings"] == 0
    statuses = {d["status"] for d in c["diff"]}
    assert "qty_change" in statuses

    # Re-counting the same section replaces the earlier count (no double counting)
    _count(client, sid2, 1)
    rep2 = client.get(f"/api/sessions/{sid2}/report").json()
    assert rep2["totals"]["units"] == sum(s["units"] for s in c["slots"])

    # Depth remembered from the map carries into the count
    assert all(s["depth"] >= 1 for s in c["slots"])


def test_count_unmapped_aisle(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/walk/count", files={"image": ("c.png", frame(0), "image/png")},
                    data={"zone": "floor", "aisle": "99"})
    assert r.status_code == 409


def test_overlap_skipped_when_snapshot_missing(client):
    client.post("/api/catalog/load-sample")
    sid = client.post("/api/sessions", json={}).json()["id"]
    _map(client, sid, 1, idx=0)
    _map(client, sid, 3, idx=1)  # seq 2 never arrived: frames are not neighbours
    secs = client.get(f"/api/sessions/{sid}/walk/sections", params={"zone": "floor", "aisle": "7"}).json()
    assert sum(s["duplicates"] for s in secs) == 0
