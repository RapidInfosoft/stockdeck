# StockDeck — camera inventory MVP

Count a liquor store by photographing shelf bays and back-room stacks. Each photo is read
by Claude vision, matched to your catalog, drawn as a **shelf blueprint**, reviewed in a
few taps, and rolled up into totals **by line item and size, loose vs case**.

This MVP is built to validate the flow end to end, not to be production ready.

## Two ways to count

### Walk mode (video): the main workflow
**Run 1, Map run (builds the database).** Open **Walk**, choose *1 · Map run*, enter the aisle, and tap Start.
Hold the phone sideways facing the shelf and walk slowly along the aisle. The camera engine:
- draws live boxes on the bottles it detects in the browser (TensorFlow.js COCO-SSD; used as guidance only),
- tracks how far the camera has moved and how sharp the picture is,
- **takes a full-resolution snapshot automatically** every time you've moved about half a frame and paused for a moment
  ("Hold still…" on screen). Neighbouring snapshots overlap by about 50%,
- uploads snapshots in the background while you keep walking.

Each snapshot becomes a section of the aisle (`F-7-S01`, `F-7-S02`, …). Claude identifies every product and size.
Groups that appear in two neighbouring snapshots are counted once. Each product is cropped and saved as a **reference photo**.
Tap **Done** to see the aisle's sections, fix anything amber or red, then **Confirm aisle map**. The aisle is now
in the database: every section's shelf blueprint (product, size, facings, rows deep) plus the reference photos.

**Run 2, Count run (uses the database).** Choose *2 · Count run*, enter the aisle, and point the camera at a section.
When the picture is steady, it snapshots and sends the photo **together with the saved aisle map**. Claude doesn't have to
re-identify 10,000 products; it only has to work out which section this is and count each mapped slot.
A result card appears within seconds: `F-7-S02 · 41 bottles · Tito's 750ml 5→3 · Patron Reposado 3→0`.
Move to the next section and repeat. Rows deep comes from the map, and recounting a section replaces its earlier count.

### Photo mode
One still photo per bay (the **Photo** tab), reviewed and confirmed bay by bay. Good for back-room stacks and spot checks.

### Review, report
- In the blueprint, green = counted, amber = check, red = unknown product, dashed = overlap already counted.
  Tap a slot to fix it. The editor shows reference photos of the product.
- **Report**: totals per product, with each size under it: floor loose, back-room loose, cases × pack, total. Exports to CSV.

## Run it on Replit

1. In Replit: **Create App → Import from GitHub**, then choose `RapidInfosoft/stockdeck`.
2. Open **Secrets** (padlock icon) and add:
   - `ANTHROPIC_API_KEY`: your key from console.anthropic.com
   - `STOCKDECK_PASSWORD`: any password. The URL is public, so this keeps strangers out.
3. Press **Run**. Open the web preview URL on your phone. When asked to log in, use any username and the password above.
4. On the **Catalog** tab, load the sample catalog or import your POS CSV, then start scanning.

For use beyond testing, **Deploy → Reserved VM** keeps the app running with the same secrets. Use a Reserved VM,
not Autoscale: the database and photos are files on disk, and Autoscale doesn't keep files between restarts.

## Run it locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # without a key the app runs in demo mode (canned scan)
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<computer-ip>:8000` on a phone on the same Wi-Fi network. Phones only allow live camera access
(used for QR scanning) over HTTPS. The **Photograph this bay** button uses the phone's camera app and works
over plain HTTP.

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Enables real recognition |
| `STOCKDECK_RECOGNIZER` | `auto` | `claude`, `mock`, or `auto` (Claude if a key exists) |
| `STOCKDECK_MODEL` | `claude-opus-5-5` | Vision model |
| `STOCKDECK_DATA` | `./var` | SQLite database and stored photos |
| `STOCKDECK_PASSWORD` | — | If set, the app asks for this password (any username) |

Tests: `pip install -r requirements-dev.txt && pytest`

## Layout

```
backend/app/
  recognizer.py   photo -> ShelfScan (Claude vision with structured output, or mock)
  matcher.py      read text -> catalog SKU (UPC > name similarity gated by size; ambiguity -> review)
  inventory.py    slots, blueprint diff/confirm, session report math
  main.py         FastAPI endpoints + serves the web app
  static/         mobile web UI (no build step)
    walk.js       live camera engine: motion/sharpness auto-snapshots, on-device bottle boxes, upload queue
    vendor/       TensorFlow.js + COCO-SSD (bundled, Apache-2.0)
data/sample_catalog.csv
docs/ARCHITECTURE.md   blueprint design and the path from MVP to production
```
