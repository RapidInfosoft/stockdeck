# StockDeck — camera inventory MVP

Count a liquor store by photographing shelf bays and back-room stacks. Each photo is read
by Claude vision, matched to your catalog, drawn as a **shelf blueprint**, reviewed in a
few taps, and rolled up into totals **by line item and size, loose vs case**.

This MVP is built to validate the flow end to end, not to be production ready.

## The flow

1. **Catalog**: import your POS export as CSV (`upc, brand, name, category, size_ml, case_pack, case_upc`,
   one row per size), or load the 44-item sample.
2. **Scan**: pick *Sales floor* or *Back room*, enter aisle and bay (or scan a QR label like `F-3-2`),
   and photograph the whole bay.
3. **Review**: the photo shows numbered boxes. Under it, the blueprint shows each shelf left to right,
   with one slot per product and size.
   - green = counted automatically · amber = check me · red = product not identified
   - tap a slot to change the product, facings, rows deep, cases, bottles per case, or loose bottles in an open case
   - **+ Add item** covers anything the camera missed (search by name or type the UPC)
4. **Confirm bay**: locks the count and saves the layout as that bay's **blueprint**. The bay number moves to the next bay.
5. **Report**: totals per product, with each size under it: floor loose, back-room loose, cases × pack,
   case bottles, total. Export as CSV.
6. **Next count**: when you rescan a bay, it's compared with its blueprint (same / moved / facings changed /
   missing / new). Rows-deep values you confirmed before are carried forward.

Rescanning a bay in the same count replaces the earlier photo, so the bay is never counted twice.

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
data/sample_catalog.csv
docs/ARCHITECTURE.md   blueprint design and the path from MVP to production
```
