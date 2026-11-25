"""Turn a shelf / back-room photo into a structured list of product groups.

Two implementations share one output schema (`ShelfScan`):

* ClaudeRecognizer – sends the photo to Claude vision and gets back validated JSON.
* MockRecognizer   – returns a canned scan so the whole flow can be exercised
                     without an API key (demo mode and automated tests).

The recognizer only *reads* the photo. Mapping what it read onto catalog SKUs is
done afterwards by `matcher.py`, so the model never has to know the 10k-item catalog.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Literal, Optional, Protocol

from PIL import Image, ImageOps
from pydantic import BaseModel, Field

try:  # iPhones can upload HEIC photos; Pillow needs this plugin to open them.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - optional dependency
    pass

MODEL = os.environ.get("STOCKDECK_MODEL", "claude-opus-5-5")
MAX_IMAGE_SIDE = 2000  # px; keeps label text legible while bounding upload size and tokens


class Box(BaseModel):
    """Bounding box in normalised image coordinates (0..1, origin top-left)."""

    x0: float
    y0: float
    x1: float
    y1: float


class DetectedItem(BaseModel):
    shelf: int = Field(description="Shelf number counted from the TOP of the photo, starting at 1. For back-room stacks use the rack level or 1 for floor stacks.")
    position: int = Field(description="Left-to-right order of this group on its shelf, starting at 1.")
    kind: Literal["bottle", "case"]
    brand: str = Field(description="Brand as printed, e.g. \"Tito's\". Empty string if unreadable.")
    product: str = Field(description="Product / variant name as printed, e.g. \"Handmade Vodka\", \"Reposado\".")
    size_text: str = Field(description="Size exactly as printed on label, shelf tag or case, e.g. \"750 mL\", \"1.75L\", \"12/750ML\". Empty if not readable.")
    size_ml: Optional[int] = Field(description="Best estimate of single-bottle volume in mL (50, 100, 200, 375, 750, 1000, 1750...). Null if unknown.")
    upc: Optional[str] = Field(description="UPC/GTIN digits only if clearly legible on a barcode, shelf tag or case; otherwise null.")
    facings: int = Field(description="Bottles: number of identical bottles side by side in the FRONT row. Cases: number of case faces visible.")
    depth_visible: Optional[int] = Field(description="Bottles: rows deep, only if bottles behind the front row can actually be seen; otherwise null.")
    cases: Optional[int] = Field(description="Cases: estimated total sealed cases in this stack including hidden ones implied by a regular stack shape. Null for bottles.")
    units_per_case: Optional[int] = Field(description="Cases: bottles per case if printed (e.g. 12 from \"12/750ML\"); otherwise null.")
    open_case_units: Optional[int] = Field(description="Cases: loose bottles visible in an opened case of this product; otherwise null.")
    confidence: float = Field(description="0..1 confidence that brand+product+size are correct.")
    box: Box
    notes: str = Field(description="Short note on anything uncertain (e.g. \"size inferred from shape\"). Empty if none.")


class ShelfScan(BaseModel):
    shelf_count: int = Field(description="Number of shelves (or rack levels) visible.")
    image_quality: Literal["good", "blurry", "partial", "too_dark", "glare"]
    warnings: list[str] = Field(description="Problems that may affect the count, e.g. \"right edge cut off\".")
    items: list[DetectedItem]


class CountedSlot(BaseModel):
    ref: Optional[str] = Field(description="The slot reference exactly as listed in the aisle map (e.g. \"F-3-S02/s2p1\"), or null for a product that is not in the map.")
    visible: bool = Field(description="True if that slot's shelf spot is inside the photo. A visible but EMPTY spot is visible=true with facings=0.")
    facings: int = Field(description="Bottles side by side in the front row now (cases: case faces visible). 0 if the spot is empty.")
    kind: Literal["bottle", "case"]
    shelf: int = Field(description="Shelf number from the top of THIS photo, starting at 1.")
    brand: str = Field(description="Brand read from the label (needed for unmapped products; repeat the mapped brand otherwise).")
    product: str
    size_text: str
    size_ml: Optional[int]
    cases: Optional[int] = Field(description="Cases only: total sealed cases in the stack; otherwise null.")
    units_per_case: Optional[int]
    open_case_units: Optional[int]
    confidence: float = Field(description="0..1 confidence in this slot's product and count.")
    box: Box
    notes: str


class CountScan(BaseModel):
    section: Optional[str] = Field(description="Code of the mapped section this photo shows (e.g. \"F-3-S02\"), or null if it matches none.")
    section_confidence: float = Field(description="0..1 confidence in the section choice.")
    image_quality: Literal["good", "blurry", "partial", "too_dark", "glare"]
    warnings: list[str]
    slots: list[CountedSlot]


PROMPT_COMMON = """You are counting inventory in a liquor store from a single photo.
Return every distinct product group you can see. A product group is one product in one size,
standing together on one shelf (or one stack of identical cases).

Rules:
- Different sizes of the same product are DIFFERENT groups (750 mL vs 1.75 L vs 50 mL).
- Use every clue to identify product and size: front label text, neck labels, shelf price tags
  directly under the group (they usually print name, size and UPC), bottle shape, cap colour.
  Bottle height relative to neighbours and to the shelf tag helps tell 375/750/1000/1750 mL apart.
- Count only what is visible. Never invent bottles you cannot see.
- If the same product appears in two separated spots on one shelf, report two groups.
- Give a tight bounding box around each group.
- Lower confidence honestly when text is unreadable or the size is inferred.
"""

PROMPT_FLOOR = PROMPT_COMMON + """
This photo is of a SALES-FLOOR shelf bay. Items are loose bottles (kind = "bottle").
Number shelves from the top of the photo. `facings` = bottles side by side in the front row.
Only fill `depth_visible` if rows behind the front bottle are genuinely visible.
"""

PROMPT_BACKROOM = PROMPT_COMMON + """
This photo is of the BACK ROOM. Expect sealed cases (kind = "case") and possibly loose bottles
(kind = "bottle") on racks.
For cases read the printed carton text: brand, product, pack size like "12/750ML" or "6 x 1.75L".
`facings` = case faces you can see; `cases` = your estimate of total cases in that stack when the
stack is a regular block (e.g. 3 wide x 2 deep x 4 high = 24); otherwise equal to visible cases.
Report open cases via `open_case_units`.
"""


WALK_HINT = """
This photo is one frame of a walk-through video along the aisle; the next frame overlaps this one.
Product groups cut off at the left or right edge: report them and count only the bottles you can see.
"""

PROMPT_COUNT = """You are re-counting a liquor store shelf that was mapped earlier.
You get a photo and the saved map of the aisle. Each map line is one product slot:
  [ref] shelf N: Brand Product Size, F facings last time

Do this:
1. Decide which mapped section the photo shows, using the products and their arrangement. Put its code in
   `section` (null if nothing fits).
2. For every slot of that section, report the CURRENT front-row facings with its exact `ref`.
   If the spot is in the photo but empty (sold out), facings = 0 and visible = true.
   If the spot is outside the photo, visible = false.
3. Slots from neighbouring sections that are also fully visible may be reported with their own ref.
4. Products in the photo that match no slot: report them with ref = null (read brand, product, size).
Count only what you can see. Products have usually not moved, but report what is really there now.
"""


class Recognizer(Protocol):
    name: str

    def scan(self, image_bytes: bytes, zone: str, walk: bool = False) -> ShelfScan: ...

    def count(self, image_bytes: bytes, zone: str, aisle_map: str, sections: list[dict]) -> CountScan: ...


def prepare_image(image_bytes: bytes) -> tuple[bytes, str]:
    """Fix phone EXIF rotation and downscale; returns JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue(), "image/jpeg"


class RecognitionError(RuntimeError):
    pass


class ClaudeRecognizer:
    name = "claude"

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def scan(self, image_bytes: bytes, zone: str, walk: bool = False) -> ShelfScan:
        prompt = PROMPT_BACKROOM if zone == "backroom" else PROMPT_FLOOR
        if walk:
            prompt += WALK_HINT
        return self._ask(image_bytes, prompt, "List every product group in this photo.", ShelfScan, "medium")

    def count(self, image_bytes: bytes, zone: str, aisle_map: str, sections: list[dict]) -> CountScan:
        text = f"Saved map of this aisle:\n{aisle_map}\n\nCount the slots shown in this photo."
        # Low effort: the map does most of the work, and point-and-count should feel quick.
        return self._ask(image_bytes, PROMPT_COUNT, text, CountScan, "low")

    def _ask(self, image_bytes: bytes, system: str, text: str, schema, effort: str):
        anthropic = self._anthropic
        data, media_type = prepare_image(image_bytes)
        try:
            response = self.client.beta.messages.parse(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": effort},
                # Server-side fallback: if a safety classifier declines, the API retries on a
                # fallback model inside the same call instead of returning nothing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.standard_b64encode(data).decode(),
                                },
                            },
                            {"type": "text", "text": text},
                        ],
                    }
                ],
                output_format=schema,
            )
        except anthropic.AuthenticationError as e:
            raise RecognitionError("Claude API key is invalid or missing.") from e
        except anthropic.RateLimitError as e:
            raise RecognitionError("Rate limited by the Claude API; try again in a minute.") from e
        except anthropic.APIStatusError as e:
            raise RecognitionError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise RecognitionError("Could not reach the Claude API (network).") from e

        if response.stop_reason == "refusal":
            raise RecognitionError("The model declined to analyse this image.")
        if response.stop_reason == "max_tokens":
            raise RecognitionError("Too many items in one photo; photograph a smaller section.")
        result = response.parsed_output
        if result is None:
            raise RecognitionError("Model returned no structured result.")
        return result


FIXTURES = Path(__file__).parent / "fixtures"


class MockRecognizer:
    """Deterministic stand-in for demos and tests. Ignores the image content."""

    name = "mock"

    def scan(self, image_bytes: bytes, zone: str, walk: bool = False) -> ShelfScan:
        prepare_image(image_bytes)  # still validate that the upload is an image
        if walk:
            return self._walk_frame(image_bytes)
        fixture = FIXTURES / ("mock_backroom.json" if zone == "backroom" else "mock_floor.json")
        return ShelfScan.model_validate(json.loads(fixture.read_text()))

    def _walk_frame(self, image_bytes: bytes) -> ShelfScan:
        """Walk-through demo: an aisle of overlapping frames. The frame index is taken from
        the image's top-left pixel brightness so tests can choose it (else frame 0)."""
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        frame = img.getpixel((0, 0)) // 40  # 0..6
        aisle = json.loads((FIXTURES / "mock_walk.json").read_text())
        return ShelfScan.model_validate(aisle[frame % len(aisle)])

    def count(self, image_bytes: bytes, zone: str, aisle_map: str, sections: list[dict]) -> CountScan:
        """Pretend the photo shows the first mapped section; one item sold 1 facing, one sold out."""
        prepare_image(image_bytes)
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        if not sections:
            return CountScan(section=None, section_confidence=0, image_quality="good", warnings=[], slots=[])
        sec = sections[(img.getpixel((0, 0)) // 40) % len(sections)]
        slots = []
        for i, s in enumerate(sec["slots"]):
            facings = s["facings"] - 1 if i == 0 else (0 if i == 1 else s["facings"])
            slots.append(CountedSlot(
                ref=s["ref"], visible=True, facings=max(0, facings), kind=s["kind"], shelf=s["shelf"],
                brand=s["brand"], product=s["name"], size_text="", size_ml=s["size_ml"], cases=None,
                units_per_case=None, open_case_units=None, confidence=0.9,
                box=Box(x0=min(0.9, i * 0.15), y0=0.1, x1=min(1.0, i * 0.15 + 0.14), y1=0.3), notes="",
            ))
        return CountScan(section=sec["code"], section_confidence=0.9, image_quality="good", warnings=[], slots=slots)


def has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_recognizer() -> Recognizer:
    """STOCKDECK_RECOGNIZER=claude|mock; default picks claude when credentials exist."""
    choice = os.environ.get("STOCKDECK_RECOGNIZER", "auto").lower()
    if choice == "mock" or (choice == "auto" and not has_claude_credentials()):
        return MockRecognizer()
    return ClaudeRecognizer()
