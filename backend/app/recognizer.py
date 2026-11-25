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


class Recognizer(Protocol):
    name: str

    def scan(self, image_bytes: bytes, zone: str) -> ShelfScan: ...


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

    def scan(self, image_bytes: bytes, zone: str) -> ShelfScan:
        anthropic = self._anthropic
        data, media_type = prepare_image(image_bytes)
        prompt = PROMPT_BACKROOM if zone == "backroom" else PROMPT_FLOOR
        try:
            response = self.client.beta.messages.parse(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": "medium"},
                # Server-side fallback: if a safety classifier declines, the API retries on a
                # fallback model inside the same call instead of returning nothing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=prompt,
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
                            {"type": "text", "text": "List every product group in this photo."},
                        ],
                    }
                ],
                output_format=ShelfScan,
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
        scan = response.parsed_output
        if scan is None:
            raise RecognitionError("Model returned no structured result.")
        return scan


FIXTURES = Path(__file__).parent / "fixtures"


class MockRecognizer:
    """Deterministic stand-in for demos and tests. Ignores the image content."""

    name = "mock"

    def scan(self, image_bytes: bytes, zone: str) -> ShelfScan:
        prepare_image(image_bytes)  # still validate that the upload is an image
        fixture = FIXTURES / ("mock_backroom.json" if zone == "backroom" else "mock_floor.json")
        return ShelfScan.model_validate(json.loads(fixture.read_text()))


def has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_recognizer() -> Recognizer:
    """STOCKDECK_RECOGNIZER=claude|mock; default picks claude when credentials exist."""
    choice = os.environ.get("STOCKDECK_RECOGNIZER", "auto").lower()
    if choice == "mock" or (choice == "auto" and not has_claude_credentials()):
        return MockRecognizer()
    return ClaudeRecognizer()
