"""StockDeck MVP API + static mobile web app.

Run:  uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .inventory import (aisle_map, capture_detail, confirm_capture, recompute_overlaps, renumber_positions,
                        session_report, store_count, store_scan)
from .matcher import parse_size_ml, size_label
from .recognizer import RecognitionError, get_recognizer, prepare_image

STATIC = Path(__file__).parent / "static"
SAMPLE_CATALOG = Path(__file__).resolve().parents[2] / "data" / "sample_catalog.csv"
MAX_UPLOAD = 25 * 1024 * 1024

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="StockDeck", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """Optional shared password (HTTP Basic). Set STOCKDECK_PASSWORD when the app is on a public URL.

    Any username is accepted; the browser remembers it after the first prompt.
    """
    password = os.environ.get("STOCKDECK_PASSWORD")
    if password:
        supplied = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        if not hmac.compare_digest(supplied.encode(), password.encode()):
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="StockDeck"'})
    return await call_next(request)


# ---------------------------------------------------------------- status

@app.get("/api/status")
def status():
    rec = get_recognizer()
    with db.tx() as conn:
        n = conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0]
    return {"recognizer": rec.name, "catalog_size": n}


# ---------------------------------------------------------------- catalog

CATALOG_COLUMNS = ["upc", "brand", "name", "category", "size_ml", "case_pack", "case_upc"]


def import_catalog_csv(text: str) -> dict:
    """Upsert SKUs by UPC. Accepts size_ml as a number or text like '1.75L'."""
    reader = csv.DictReader(io.StringIO(text))
    missing = {"brand", "name"} - set(reader.fieldnames or [])
    if missing:
        raise HTTPException(400, f"CSV missing columns: {', '.join(sorted(missing))}")
    added = updated = skipped = 0
    with db.tx() as conn:
        for row in reader:
            brand, name = (row.get("brand") or "").strip(), (row.get("name") or "").strip()
            if not brand or not name:
                skipped += 1
                continue
            size = row.get("size_ml") or row.get("size") or ""
            size_ml = int(size) if str(size).strip().isdigit() else parse_size_ml(size)
            case_pack = int(row["case_pack"]) if (row.get("case_pack") or "").strip().isdigit() else None
            upc = (row.get("upc") or "").strip() or None
            vals = (brand, name, (row.get("category") or "").strip(), size_ml, case_pack,
                    (row.get("case_upc") or "").strip() or None)
            existing = conn.execute("SELECT id FROM skus WHERE upc = ?", (upc,)).fetchone() if upc else None
            if existing:
                conn.execute(
                    "UPDATE skus SET brand=?, name=?, category=?, size_ml=?, case_pack=?, case_upc=? WHERE id=?",
                    (*vals, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO skus (brand, name, category, size_ml, case_pack, case_upc, upc) VALUES (?,?,?,?,?,?,?)",
                    (*vals, upc),
                )
                added += 1
    return {"added": added, "updated": updated, "skipped": skipped}


@app.post("/api/catalog/import")
async def catalog_import(file: UploadFile = File(...)):
    raw = await file.read()
    return import_catalog_csv(raw.decode("utf-8-sig"))


@app.post("/api/catalog/load-sample")
def catalog_load_sample():
    return import_catalog_csv(SAMPLE_CATALOG.read_text())


@app.get("/api/catalog")
def catalog_list(q: str = "", limit: int = 50):
    with db.tx() as conn:
        like = f"%{q.strip()}%"
        res = db.rows(conn.execute(
            """SELECT * FROM skus WHERE brand || ' ' || name || ' ' || COALESCE(upc,'') LIKE ?
               ORDER BY brand, name, size_ml LIMIT ?""",
            (like, limit),
        ))
    for r in res:
        r["size"] = size_label(r["size_ml"])
    return res


class SkuIn(BaseModel):
    brand: str
    name: str
    size_ml: int
    category: str = ""
    upc: Optional[str] = None
    case_pack: Optional[int] = None


@app.post("/api/catalog")
def catalog_create(sku: SkuIn):
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO skus (brand, name, size_ml, category, upc, case_pack) VALUES (?,?,?,?,?,?)",
            (sku.brand.strip(), sku.name.strip(), sku.size_ml, sku.category, sku.upc or None, sku.case_pack),
        )
        return dict(conn.execute("SELECT * FROM skus WHERE id = ?", (cur.lastrowid,)).fetchone())


# ---------------------------------------------------------------- sessions

class SessionIn(BaseModel):
    name: Optional[str] = None


@app.post("/api/sessions")
def session_create(body: SessionIn):
    name = body.name or f"Count {date.today().isoformat()}"
    with db.tx() as conn:
        cur = conn.execute("INSERT INTO count_sessions (name) VALUES (?)", (name,))
        return dict(conn.execute("SELECT * FROM count_sessions WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.get("/api/sessions")
def session_list():
    with db.tx() as conn:
        return db.rows(conn.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM captures c WHERE c.session_id = s.id AND c.status != 'superseded') AS bays
               FROM count_sessions s ORDER BY s.id DESC"""
        ))


@app.get("/api/sessions/{session_id}/captures")
def session_captures(session_id: int):
    with db.tx() as conn:
        return db.rows(conn.execute(
            """SELECT c.id, c.status, c.created_at, l.code, l.zone,
                      (SELECT COUNT(*) FROM slots WHERE capture_id = c.id) AS items,
                      (SELECT COUNT(*) FROM slots WHERE capture_id = c.id AND review = 'needs_review') AS needs_review
               FROM captures c JOIN locations l ON l.id = c.location_id
               WHERE c.session_id = ? AND c.status != 'superseded' ORDER BY c.id DESC""",
            (session_id,),
        ))


# ---------------------------------------------------------------- captures

async def _read_upload(image: UploadFile) -> bytes:
    data = await image.read()
    if not data:
        raise HTTPException(400, "Empty image upload")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large (25 MB max)")
    return data


def _check_session(session_id: int) -> None:
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM count_sessions WHERE id = ?", (session_id,)).fetchone():
            raise HTTPException(404, "Session not found")


def _prepared(data: bytes) -> bytes:
    try:
        return prepare_image(data)[0]
    except OSError as e:  # PIL could not read it
        raise HTTPException(400, f"Not a readable image: {e}") from e


def _save_image(jpeg: bytes) -> Path:
    # Store the upright, resized JPEG the recognizer saw, so boxes line up and any browser can show it.
    path = db.data_dir() / "images" / f"{uuid.uuid4().hex}.jpg"
    path.write_bytes(jpeg)
    return path


def _new_capture(conn, session_id: int, location_id: int, path: Path, recognizer: str, quality: str,
                 warnings: list, raw: str, mode: str, seq: Optional[int] = None) -> int:
    # A rescan of the same spot in the same session replaces the earlier capture.
    conn.execute(
        "UPDATE captures SET status = 'superseded' WHERE session_id = ? AND location_id = ? AND status != 'superseded'",
        (session_id, location_id),
    )
    cur = conn.execute(
        """INSERT INTO captures (session_id, location_id, image_path, recognizer, image_quality, warnings, raw_json, mode, seq)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (session_id, location_id, str(path), recognizer, quality, json.dumps(warnings), raw, mode, seq),
    )
    return cur.lastrowid


@app.post("/api/sessions/{session_id}/captures")
async def capture_create(
    session_id: int,
    image: UploadFile = File(...),
    zone: Literal["floor", "backroom"] = Form(...),
    aisle: str = Form(...),
    bay: str = Form(...),
):
    """Photo mode: one still photo of one bay."""
    data = await _read_upload(image)
    _check_session(session_id)
    jpeg = _prepared(data)
    recognizer = get_recognizer()
    try:
        scan = recognizer.scan(data, zone)
    except RecognitionError as e:
        raise HTTPException(502, str(e)) from e
    path = _save_image(jpeg)
    with db.tx() as conn:
        loc = db.get_or_create_location(conn, zone, aisle, bay)
        cid = _new_capture(conn, session_id, loc["id"], path, recognizer.name, scan.image_quality,
                           scan.warnings, scan.model_dump_json(), "photo")
        store_scan(conn, cid, loc["id"], scan, jpeg)
        return capture_detail(conn, cid)


# ---------------------------------------------------------------- walk-through: map run & count run

def _section_bay(seq: int) -> str:
    return f"S{seq:02d}"


@app.post("/api/sessions/{session_id}/walk/map")
async def walk_map(
    session_id: int,
    image: UploadFile = File(...),
    zone: Literal["floor", "backroom"] = Form(...),
    aisle: str = Form(...),
    seq: int = Form(...),
):
    """Run 1 (build the database): one auto-snapshot from the walk-through video.
    Each snapshot becomes a numbered section of the aisle (F-3-S01, F-3-S02, ...)."""
    if seq < 1 or seq > 999:
        raise HTTPException(400, "seq must be 1..999")
    data = await _read_upload(image)
    _check_session(session_id)
    jpeg = _prepared(data)
    recognizer = get_recognizer()
    try:
        scan = recognizer.scan(data, zone, walk=True)
    except RecognitionError as e:
        raise HTTPException(502, str(e)) from e
    path = _save_image(jpeg)
    with db.tx() as conn:
        loc = db.get_or_create_location(conn, zone, aisle, _section_bay(seq))
        cid = _new_capture(conn, session_id, loc["id"], path, recognizer.name, scan.image_quality,
                           scan.warnings, scan.model_dump_json(), "map", seq)
        store_scan(conn, cid, loc["id"], scan, jpeg)
        recompute_overlaps(conn, session_id, db.location_code(zone, aisle, "S"))
        return capture_detail(conn, cid)


@app.post("/api/sessions/{session_id}/walk/count")
async def walk_count(
    session_id: int,
    image: UploadFile = File(...),
    zone: Literal["floor", "backroom"] = Form(...),
    aisle: str = Form(...),
):
    """Run 2 (count): point the camera at any mapped section. The saved map tells the model what
    should be there, so it only has to find the section and count facings."""
    data = await _read_upload(image)
    _check_session(session_id)
    jpeg = _prepared(data)
    with db.tx() as conn:
        map_text, sections = aisle_map(conn, zone, aisle)
    if not sections:
        raise HTTPException(409, f"Aisle {aisle} has no confirmed map yet. Do a map run first and confirm it.")
    recognizer = get_recognizer()
    try:
        scan = recognizer.count(data, zone, map_text, sections)
    except RecognitionError as e:
        raise HTTPException(502, str(e)) from e
    section = next((s for s in sections if s["code"] == scan.section), None)
    if not section or scan.section_confidence < 0.4:
        raise HTTPException(422, "Couldn't tell which mapped section this is. Step back so more of the shelf is in view.")
    path = _save_image(jpeg)
    with db.tx() as conn:
        cid = _new_capture(conn, session_id, section["location_id"], path, recognizer.name, scan.image_quality,
                           scan.warnings, scan.model_dump_json(), "count")
        store_count(conn, cid, scan, sections, jpeg)
        return capture_detail(conn, cid)


@app.get("/api/sessions/{session_id}/walk/sections")
def walk_sections(session_id: int, zone: Literal["floor", "backroom"], aisle: str):
    prefix = db.location_code(zone, aisle, "S")
    with db.tx() as conn:
        caps = db.rows(conn.execute(
            """SELECT c.id, c.seq, c.status, l.code,
                      (SELECT COUNT(*) FROM slots s WHERE s.capture_id = c.id AND s.duplicate_of IS NULL) AS items,
                      (SELECT COUNT(*) FROM slots s WHERE s.capture_id = c.id AND s.duplicate_of IS NOT NULL) AS duplicates,
                      (SELECT COUNT(*) FROM slots s WHERE s.capture_id = c.id AND s.duplicate_of IS NULL
                         AND (s.review = 'needs_review' OR s.sku_id IS NULL)) AS needs_review
               FROM captures c JOIN locations l ON l.id = c.location_id
               WHERE c.session_id = ? AND c.mode = 'map' AND c.status != 'superseded' AND l.code LIKE ?
               ORDER BY c.seq""",
            (session_id, prefix + "%"),
        ))
    return caps


@app.post("/api/sessions/{session_id}/walk/confirm")
def walk_confirm(session_id: int, zone: Literal["floor", "backroom"], aisle: str):
    """Confirm every section of this map run; the aisle's saved map becomes exactly these sections."""
    prefix = db.location_code(zone, aisle, "S")
    with db.tx() as conn:
        caps = conn.execute(
            """SELECT c.id, l.code FROM captures c JOIN locations l ON l.id = c.location_id
               WHERE c.session_id = ? AND c.mode = 'map' AND c.status != 'superseded' AND l.code LIKE ?
               ORDER BY c.seq""",
            (session_id, prefix + "%"),
        ).fetchall()
        if not caps:
            raise HTTPException(404, "No map snapshots for this aisle in this count")
        problems = []
        for c in caps:
            n = conn.execute(
                "SELECT COUNT(*) FROM slots WHERE capture_id = ? AND sku_id IS NULL AND duplicate_of IS NULL", (c["id"],)
            ).fetchone()[0]
            if n:
                problems.append(f"{c['code']}: {n} unidentified")
        if problems:
            raise HTTPException(409, "Assign or delete unidentified items first — " + "; ".join(problems))
        for c in caps:
            confirm_capture(conn, c["id"])
        # Sections from an older, longer walk of this aisle no longer exist.
        codes = [c["code"] for c in caps]
        conn.execute(
            f"""DELETE FROM blueprint_slots WHERE location_id IN (
                  SELECT id FROM locations WHERE code LIKE ? AND code NOT IN ({','.join('?' * len(codes))}))""",
            (prefix + "%", *codes),
        )
    return {"confirmed": len(caps), "sections": codes}


@app.get("/api/slots/{slot_id}/crop")
def slot_crop(slot_id: int):
    with db.tx() as conn:
        row = conn.execute("SELECT crop_path FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if not row or not row["crop_path"] or not Path(row["crop_path"]).exists():
        raise HTTPException(404, "No reference photo")
    return FileResponse(row["crop_path"])


@app.get("/api/skus/{sku_id}/references")
def sku_references(sku_id: int, limit: int = 6):
    """Reference photos collected for a product (confirmed slots first)."""
    with db.tx() as conn:
        return db.rows(conn.execute(
            """SELECT id AS slot_id, review FROM slots WHERE sku_id = ? AND crop_path IS NOT NULL
               ORDER BY review = 'confirmed' DESC, id DESC LIMIT ?""",
            (sku_id, limit),
        ))


@app.get("/api/captures/{capture_id}")
def capture_get(capture_id: int):
    with db.tx() as conn:
        cap = capture_detail(conn, capture_id)
    if not cap:
        raise HTTPException(404, "Capture not found")
    return cap


@app.get("/api/captures/{capture_id}/image")
def capture_image(capture_id: int):
    with db.tx() as conn:
        row = conn.execute("SELECT image_path FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if not row or not Path(row["image_path"]).exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(row["image_path"])


@app.post("/api/captures/{capture_id}/confirm")
def capture_confirm(capture_id: int):
    with db.tx() as conn:
        try:
            confirm_capture(conn, capture_id)
        except KeyError:
            raise HTTPException(404, "Capture not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return capture_detail(conn, capture_id)


class SlotPatch(BaseModel):
    sku_id: Optional[int] = None
    facings: Optional[int] = None
    depth: Optional[int] = None
    cases: Optional[int] = None
    units_per_case: Optional[int] = None
    partial_units: Optional[int] = None
    shelf: Optional[int] = None


def _editable_slot(conn, slot_id: int) -> dict:
    row = conn.execute(
        "SELECT sl.*, c.status AS capture_status FROM slots sl JOIN captures c ON c.id = sl.capture_id WHERE sl.id = ?",
        (slot_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Slot not found")
    if row["capture_status"] == "superseded":
        raise HTTPException(409, "This capture was replaced by a rescan")
    return dict(row)


@app.patch("/api/slots/{slot_id}")
def slot_update(slot_id: int, patch: SlotPatch):
    changes = patch.model_dump(exclude_unset=True)
    for k in ("facings", "cases", "partial_units"):
        if k in changes and (changes[k] is None or changes[k] < 0):
            raise HTTPException(400, f"{k} must be >= 0")
    for k in ("depth", "shelf"):
        if k in changes and (changes[k] is None or changes[k] < 1):
            raise HTTPException(400, f"{k} must be >= 1")
    with db.tx() as conn:
        slot = _editable_slot(conn, slot_id)
        if changes.get("sku_id") is not None and not conn.execute(
            "SELECT 1 FROM skus WHERE id = ?", (changes["sku_id"],)
        ).fetchone():
            raise HTTPException(400, "Unknown sku_id")
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE slots SET {sets}, review = 'confirmed' WHERE id = ?", (*changes.values(), slot_id))
            if "shelf" in changes:
                renumber_positions(conn, slot["capture_id"])
        return capture_detail(conn, slot["capture_id"])


@app.delete("/api/slots/{slot_id}")
def slot_delete(slot_id: int):
    with db.tx() as conn:
        slot = _editable_slot(conn, slot_id)
        conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        renumber_positions(conn, slot["capture_id"])
        return capture_detail(conn, slot["capture_id"])


class SlotIn(BaseModel):
    sku_id: int
    kind: Literal["bottle", "case"] = "bottle"
    shelf: int = 1
    facings: int = 1
    depth: int = 1
    cases: int = 0
    partial_units: int = 0


@app.post("/api/captures/{capture_id}/slots")
def slot_add(capture_id: int, body: SlotIn):
    """Manual add, e.g. something the camera missed or a barcode scanned by hand."""
    with db.tx() as conn:
        cap = conn.execute("SELECT status FROM captures WHERE id = ?", (capture_id,)).fetchone()
        if not cap:
            raise HTTPException(404, "Capture not found")
        sku = conn.execute("SELECT * FROM skus WHERE id = ?", (body.sku_id,)).fetchone()
        if not sku:
            raise HTTPException(400, "Unknown sku_id")
        conn.execute(
            """INSERT INTO slots (capture_id, kind, shelf, position, sku_id, facings, depth, cases,
                   units_per_case, partial_units, confidence, match_score, source, review)
               VALUES (?,?,?,0,?,?,?,?,?,?,1,1,'manual','confirmed')""",
            (capture_id, body.kind, max(1, body.shelf), body.sku_id, max(0, body.facings), max(1, body.depth),
             max(0, body.cases), sku["case_pack"] if body.kind == "case" else None, max(0, body.partial_units)),
        )
        renumber_positions(conn, capture_id)
        return capture_detail(conn, capture_id)


# ---------------------------------------------------------------- blueprint & report

@app.get("/api/locations")
def locations():
    with db.tx() as conn:
        return db.rows(conn.execute(
            """SELECT l.*, (SELECT COUNT(*) FROM blueprint_slots b WHERE b.location_id = l.id) AS blueprint_slots
               FROM locations l ORDER BY zone, aisle, bay"""
        ))


@app.get("/api/locations/{location_id}/blueprint")
def location_blueprint(location_id: int):
    from .inventory import blueprint_for

    with db.tx() as conn:
        return blueprint_for(conn, location_id)


@app.get("/api/sessions/{session_id}/report")
def report(session_id: int):
    with db.tx() as conn:
        return session_report(conn, session_id)


@app.get("/api/sessions/{session_id}/report.csv", response_class=PlainTextResponse)
def report_csv(session_id: int):
    with db.tx() as conn:
        rep = session_report(conn, session_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["upc", "brand", "name", "size", "loose_floor", "loose_backroom", "cases", "case_pack",
                "case_units", "total_units", "locations", "unconfirmed"])
    for i in rep["items"]:
        w.writerow([i["upc"], i["brand"], i["name"], i["size"], i["loose_floor"], i["loose_backroom"], i["cases"],
                    i["case_pack"], i["case_units"], i["total_units"], " ".join(i["locations"]),
                    "yes" if i["unconfirmed"] else ""])
    return PlainTextResponse(
        out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="stockdeck-session-{session_id}.csv"'},
    )


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
