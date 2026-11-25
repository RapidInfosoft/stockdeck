# StockDeck architecture: shelf blueprint + bottle identification

## 1. Core idea

Every bottle is counted **at a location**: zone (floor / back room) → aisle → bay → shelf → position.
The confirmed layout of each bay is its **blueprint** (like a planogram, but recorded from reality).
The blueprint does three jobs:

1. **Identification prior.** On the next scan we already expect a certain product and size on shelf 2
   at position 3, which turns "pick 1 of 10,000 products" into "confirm or reject the expected one".
2. **Depth memory.** A camera only sees the front bottle of each row. Rows deep is confirmed once per
   slot and carried into future counts.
3. **Change detection.** A rescan reports moved, missing, new, and facings-changed slots against the blueprint.

## 2. Pipeline (MVP as built)

```
 Phone browser                     Server (FastAPI + SQLite)
 ─────────────                     ─────────────────────────
 zone/aisle/bay (or QR) ─┐
 photo of whole bay ─────┼─► POST /captures
                         │      1. prepare_image: EXIF rotate, ≤2000px JPEG
                         │      2. Recognizer.scan → ShelfScan (structured JSON)
                         │           items[]: shelf, position, kind bottle|case, brand, product,
                         │           size_text, size_ml, upc?, facings, depth_visible?, cases?,
                         │           units_per_case?, open_case_units?, confidence, box
                         │      3. matcher: UPC/case-UPC → name+size similarity → candidates
                         │      4. depth default: seen depth → blueprint depth → 1
                         │      5. review flag: no SKU | score < 0.80 | vision conf < 0.70
                         │      6. diff against blueprint
 review blueprint ◄──────┘
 tap-to-fix ───────────────► PATCH /slots/{id}   (or POST manual slot)
 Confirm bay ──────────────► POST /confirm → slots become the bay's blueprint
 Report ───────────────────► GET /report  (by line item → size; loose/cases; floor/back room)
```

### Why the model doesn't see the catalog

Claude only reports what is printed: brand, product, size, and pack. The server matches that text against
the catalog. This scales to 10k+ SKUs, keeps each request small, and keeps decisions inspectable: every slot
stores the raw text read, the vision confidence, the match score, and the top 5 candidates.

### Matching rules (`matcher.py`)

- An exact UPC or case GTIN wins outright.
- Otherwise the score combines token overlap (both directions), sequence similarity, and a brand weight.
- **Size is a hard gate**: the wrong size multiplies the score by 0.55, so a 750ml is never silently counted as a 1.75L.
- The slot goes to review when: the size is unreadable, the runner-up is within 0.05, or only the brand was read
  and the brand has several products ("Crown Royal").
- Below 0.60 the product stays **unknown** (shown red with suggestions). A bay can't be confirmed while any product is unknown.

### Counting math

- Loose bottles: `facings × depth (+ partial)`
- Cases: `cases × bottles per case (+ loose bottles in an open case)`. Bottles per case comes from the printed pack, else the catalog's `case_pack`.
- Line total = floor loose + back-room loose + case bottles. Products in the report are grouped by brand + name, with sizes underneath.

## 2b. Walk mode: map run and count run

```
MAP RUN (build the database)                         COUNT RUN (use the database)
phone: live video                                    phone: live video
  ├ COCO-SSD bottle boxes (guidance)                   ├ steady >0.7 s → snapshot (or tap)
  ├ motion: horizontal shift of 96×72 frames           └ POST /walk/count {photo, aisle}
  ├ sharpness: Laplacian energy vs rolling max              server: aisle_map() → text list of every
  └ moved ≥ ½ frame AND still ≥0.2 s AND sharp              mapped slot [F-7-S02/s2p1] shelf 2: …
        → full-res snapshot → upload queue (3 parallel)     Claude (low effort): which section? current
server per snapshot (POST /walk/map, seq=n):                facings per ref; unmapped items separately
  Claude identifies groups → catalog match                  → slots keep the SKU from the map,
  → section F-7-S0n, slots, reference crops                   depth from the map; empty spot = 0
  → recompute_overlaps(): right-edge group of n ==           → diff vs map (sold / moved / new)
    left-edge group of n+1 (same SKU, same shelf)            → report: latest count per mapped slot
    → keep larger count, mark the other duplicate
Confirm aisle → blueprints (duplicates excluded)
```

Why snapshots and not every video frame: Claude needs sharp, high-resolution stills to read labels. Motion-gated
snapshots give full aisle coverage with about 50% overlap and about 2 photos per bay. The on-device detector runs every
frame for feedback, but it can't tell Tito's from Smirnoff, so it never decides a count.

Known limits of the MVP: overlap matching only links neighbouring snapshots (a missing snapshot breaks the chain),
shelf numbering must agree between frames, and the detector backs off on slow phones.

## 3. Data model

| Table | Purpose |
|---|---|
| `skus` | product **and size**, UPC, case GTIN, case pack |
| `locations` | `F-3-2`, `B-BR-1` … (the same codes go on QR labels) |
| `count_sessions` | one inventory count |
| `captures` | one photo / snapshot in a count; `mode` photo, map (with `seq`) or count; `review → confirmed`, or `superseded` by a rescan |
| `slots` | one product group detected on a shelf: kind, shelf, position, SKU, facings, depth, cases, pack, partial, confidence, match score, candidates, box, `duplicate_of` (overlap), `crop_path` (reference photo), `blueprint_ref` (count run) |
| `blueprint_slots` | last confirmed layout per location |

## 4. What the MVP validates

- Can a single photo per bay capture enough to identify product and size?
  Check the share of amber/red slots per bay.
- How long does review take per bay? This is the real labor cost compared with a hand count.
- Does depth memory make floor totals usable after the first count?
- Do back-room case counts (printed carton text + pack) come out right?

Suggested pilot: one aisle plus one back-room rack. Hand-count the same bays and compare per SKU.

## 5. Path to production

| Area | MVP | Production |
|---|---|---|
| Capture | one still photo per bay | video walk-through, frame selection, stitching across bays, live "slow down" feedback |
| Identification | Claude vision per photo | on-device detector + embedding search over product reference photos; Claude only for low-confidence crops |
| Prior | blueprint diff after the scan | blueprint sent with the scan ("expect X at S2·P3") to raise accuracy and cut review |
| Depth | user-confirmed, remembered | LiDAR / angled shots, plus POS on-hand for cycle-count mode |
| Barcodes | UPC search box, QR location labels | multi-barcode scanning SDK for shelf tags and case GTINs |
| Data | SQLite, local images | Postgres + object storage, multi-user, audit trail, POS sync |
| Learning | — | every review fix stored as a labelled crop for retraining / reference photos |
