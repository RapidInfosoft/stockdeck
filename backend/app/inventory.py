"""Count logic: capture -> slots, blueprint diff/confirm, and the session report."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Optional

from .matcher import AUTO_ACCEPT, CatalogIndex, parse_pack, parse_size_ml, size_label
from .recognizer import ShelfScan

MIN_VISION_CONFIDENCE = 0.70


def load_index(conn: sqlite3.Connection) -> CatalogIndex:
    return CatalogIndex(dict(r) for r in conn.execute("SELECT * FROM skus"))


def blueprint_for(conn: sqlite3.Connection, location_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT b.*, s.brand, s.name, s.size_ml FROM blueprint_slots b
               JOIN skus s ON s.id = b.sku_id WHERE b.location_id = ? ORDER BY shelf, position""",
            (location_id,),
        )
    ]


def store_scan(conn: sqlite3.Connection, capture_id: int, location_id: int, scan: ShelfScan) -> None:
    """Match each detected group to the catalog and persist it as a slot."""
    index = load_index(conn)
    prior_depth = {(b["sku_id"], b["kind"]): b["depth"] for b in blueprint_for(conn, location_id)}

    for item in sorted(scan.items, key=lambda i: (i.shelf, i.box.x0)):
        size_ml = item.size_ml or parse_size_ml(item.size_text)
        m = index.match(item.brand, item.product, size_ml, item.upc)
        sku = index.skus.get(m.sku_id) if m.sku_id else None

        if item.kind == "case":
            units_per_case = item.units_per_case or parse_pack(item.size_text) or (sku or {}).get("case_pack")
            cases = item.cases if item.cases is not None else item.facings
            depth = 1
            partial = item.open_case_units or 0
        else:
            units_per_case = None
            cases = 0
            partial = 0
            depth = item.depth_visible or prior_depth.get((m.sku_id, "bottle")) or 1

        needs_review = (
            m.sku_id is None or m.score < AUTO_ACCEPT or item.confidence < MIN_VISION_CONFIDENCE
        )
        conn.execute(
            """INSERT INTO slots (capture_id, kind, shelf, position, sku_id, raw_brand, raw_name, raw_size,
                   raw_size_ml, facings, depth, cases, units_per_case, partial_units, confidence, match_score,
                   candidates, box, source, review)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'vision', ?)""",
            (
                capture_id, item.kind, max(1, item.shelf), 0, m.sku_id, item.brand, item.product,
                item.size_text, size_ml, max(0, item.facings), max(1, depth), max(0, cases),
                units_per_case, max(0, partial), round(item.confidence, 3), m.score,
                json.dumps(m.candidates), item.box.model_dump_json(),
                "needs_review" if needs_review else "auto",
            ),
        )
    renumber_positions(conn, capture_id)


def renumber_positions(conn: sqlite3.Connection, capture_id: int) -> None:
    """Positions are left-to-right per shelf, derived from box x0 (manual slots go last)."""
    slots = conn.execute(
        "SELECT id, shelf, box FROM slots WHERE capture_id = ?", (capture_id,)
    ).fetchall()
    by_shelf: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for s in slots:
        x0 = json.loads(s["box"])["x0"] if s["box"] else 9.0 + s["id"] / 1e6
        by_shelf[s["shelf"]].append((x0, s["id"]))
    for shelf, items in by_shelf.items():
        for pos, (_, sid) in enumerate(sorted(items), start=1):
            conn.execute("UPDATE slots SET position = ? WHERE id = ?", (pos, sid))


def capture_detail(conn: sqlite3.Connection, capture_id: int) -> Optional[dict]:
    cap = conn.execute(
        """SELECT c.*, l.zone, l.aisle, l.bay, l.code FROM captures c
           JOIN locations l ON l.id = c.location_id WHERE c.id = ?""",
        (capture_id,),
    ).fetchone()
    if not cap:
        return None
    cap = dict(cap)
    cap["warnings"] = json.loads(cap["warnings"] or "[]")
    cap.pop("raw_json", None)
    slots = [
        _slot_view(dict(r))
        for r in conn.execute(
            """SELECT sl.*, s.brand, s.name, s.size_ml, s.case_pack, s.upc FROM slots sl
               LEFT JOIN skus s ON s.id = sl.sku_id WHERE sl.capture_id = ? ORDER BY shelf, position""",
            (capture_id,),
        )
    ]
    cap["slots"] = slots
    cap["shelves"] = sorted({s["shelf"] for s in slots})
    cap["diff"] = blueprint_diff(blueprint_for(conn, cap["location_id"]), slots)
    cap["has_blueprint"] = bool(blueprint_for(conn, cap["location_id"]))
    cap["needs_review"] = sum(1 for s in slots if s["review"] == "needs_review")
    return cap


def _slot_view(s: dict) -> dict:
    s["candidates"] = json.loads(s["candidates"] or "[]")
    s["box"] = json.loads(s["box"]) if s["box"] else None
    s["label"] = (
        f"{s['brand']} {s['name']}" if s.get("sku_id") else f"? {s['raw_brand']} {s['raw_name']}".strip()
    )
    s["size_label"] = size_label(s["size_ml"] if s.get("sku_id") else s.get("raw_size_ml"))
    s["units"] = slot_units(s)
    return s


def slot_units(s: dict) -> int:
    if s["kind"] == "case":
        pack = s.get("units_per_case") or s.get("case_pack") or 0
        return s["cases"] * pack + s["partial_units"]
    return s["facings"] * s["depth"] + s["partial_units"]


def blueprint_diff(blueprint: list[dict], slots: list[dict]) -> list[dict]:
    """Compare a new capture to the confirmed blueprint of the same bay.

    status: same | moved (other shelf) | qty_change (facings differ) | missing | new
    """
    if not blueprint:
        return []
    out = []
    found = defaultdict(list)
    for s in slots:
        if s.get("sku_id"):
            found[s["sku_id"]].append(s)
    used: set[int] = set()
    for b in blueprint:
        hits = [s for s in found.get(b["sku_id"], []) if s["id"] not in used]
        label = f"{b['brand']} {b['name']} {size_label(b['size_ml'])}"
        if not hits:
            out.append({"status": "missing", "label": label, "shelf": b["shelf"], "expected_facings": b["facings"]})
            continue
        same_shelf = [s for s in hits if s["shelf"] == b["shelf"]]
        hit = (same_shelf or hits)[0]
        used.add(hit["id"])
        if hit["shelf"] != b["shelf"]:
            status = "moved"
        elif hit["facings"] != b["facings"]:
            status = "qty_change"
        else:
            status = "same"
        out.append({
            "status": status, "label": label, "shelf": b["shelf"], "now_shelf": hit["shelf"],
            "expected_facings": b["facings"], "facings": hit["facings"], "slot_id": hit["id"],
        })
    bp_skus = {b["sku_id"] for b in blueprint}
    for s in slots:
        if s.get("sku_id") and s["sku_id"] not in bp_skus and s["id"] not in used:
            out.append({"status": "new", "label": f"{s['label']} {s['size_label']}", "now_shelf": s["shelf"],
                        "facings": s["facings"], "slot_id": s["id"]})
    return out


def confirm_capture(conn: sqlite3.Connection, capture_id: int) -> dict:
    """Lock the capture and make its layout the bay's blueprint."""
    cap = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if not cap:
        raise KeyError(capture_id)
    unmatched = conn.execute(
        "SELECT COUNT(*) FROM slots WHERE capture_id = ? AND sku_id IS NULL", (capture_id,)
    ).fetchone()[0]
    if unmatched:
        raise ValueError(f"{unmatched} item(s) still have no product assigned. Assign or delete them first.")
    conn.execute("UPDATE slots SET review = 'confirmed' WHERE capture_id = ?", (capture_id,))
    conn.execute("UPDATE captures SET status = 'confirmed' WHERE id = ?", (capture_id,))
    conn.execute("DELETE FROM blueprint_slots WHERE location_id = ?", (cap["location_id"],))
    conn.execute(
        """INSERT INTO blueprint_slots (location_id, kind, shelf, position, sku_id, facings, depth, capture_id)
           SELECT ?, kind, shelf, position, sku_id, facings, depth, capture_id FROM slots WHERE capture_id = ?""",
        (cap["location_id"], capture_id),
    )
    return {"capture_id": capture_id, "location_id": cap["location_id"]}


def session_report(conn: sqlite3.Connection, session_id: int) -> dict:
    """Totals by line item and size, split loose vs case and floor vs back room."""
    slots = [
        dict(r)
        for r in conn.execute(
            """SELECT sl.*, c.status AS capture_status, l.zone, l.code,
                      s.brand, s.name, s.size_ml, s.case_pack, s.upc, s.category
               FROM slots sl
               JOIN captures c ON c.id = sl.capture_id
               JOIN locations l ON l.id = c.location_id
               LEFT JOIN skus s ON s.id = sl.sku_id
               WHERE c.session_id = ? AND c.status != 'superseded'""",
            (session_id,),
        )
    ]
    lines: dict[int, dict] = {}
    unidentified = []
    for s in slots:
        if not s["sku_id"]:
            unidentified.append({"code": s["code"], "read": f"{s['raw_brand']} {s['raw_name']} {s['raw_size']}".strip(),
                                 "units": slot_units(s)})
            continue
        line = lines.setdefault(s["sku_id"], {
            "sku_id": s["sku_id"], "upc": s["upc"], "brand": s["brand"], "name": s["name"],
            "category": s["category"], "size_ml": s["size_ml"], "size": size_label(s["size_ml"]),
            "loose_floor": 0, "loose_backroom": 0, "cases": 0, "case_pack": s["case_pack"],
            "case_units": 0, "total_units": 0, "locations": set(), "unconfirmed": False,
        })
        if s["kind"] == "case":
            pack = s["units_per_case"] or s["case_pack"] or 0
            line["cases"] += s["cases"]
            line["case_units"] += s["cases"] * pack
            line["loose_" + s["zone"]] += s["partial_units"]
        else:
            line["loose_" + s["zone"]] += s["facings"] * s["depth"] + s["partial_units"]
        line["locations"].add(s["code"])
        line["unconfirmed"] |= s["capture_status"] != "confirmed"

    items = []
    for line in lines.values():
        line["loose"] = line["loose_floor"] + line["loose_backroom"]
        line["total_units"] = line["loose"] + line["case_units"]
        line["locations"] = sorted(line["locations"])
        items.append(line)
    items.sort(key=lambda x: (x["brand"].lower(), x["name"].lower(), x["size_ml"] or 0))

    # Roll-up by product across sizes ("line item"), with each size underneath.
    products: dict[tuple, dict] = {}
    for it in items:
        key = (it["brand"], it["name"])
        p = products.setdefault(key, {"brand": it["brand"], "name": it["name"], "total_units": 0, "sizes": []})
        p["total_units"] += it["total_units"]
        p["sizes"].append(it)

    caps = conn.execute(
        "SELECT status, COUNT(*) n FROM captures WHERE session_id = ? GROUP BY status", (session_id,)
    ).fetchall()
    return {
        "session_id": session_id,
        "captures": {r["status"]: r["n"] for r in caps},
        "totals": {
            "skus": len(items),
            "loose": sum(i["loose"] for i in items),
            "cases": sum(i["cases"] for i in items),
            "units": sum(i["total_units"] for i in items),
        },
        "items": items,
        "products": list(products.values()),
        "unidentified": unidentified,
    }
