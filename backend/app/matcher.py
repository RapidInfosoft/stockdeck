"""Match what the camera read (brand / product / size text) to catalog SKUs.

Order of evidence: exact UPC or case-UPC  >  name similarity gated by size.
Size is treated as a hard signal: a confident name match in the wrong size is
pushed to review instead of being silently counted against the wrong SKU.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

AUTO_ACCEPT = 0.80     # match_score at/above this is counted without review
ASSIGN_MIN = 0.60      # below this the slot stays "unknown product" (suggestions only)
MIN_CANDIDATE = 0.35   # below this, a SKU is not offered as a suggestion
AMBIGUITY_GAP = 0.05   # runner-up this close to the best -> send to review

STOPWORDS = {"the", "and", "of", "a", "ml", "l", "liter", "litre", "year", "yr", "old", "no"}

_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|mls|l|lt|ltr|liter|litre|liters|litres|cl|oz)\b", re.I)
_PACK_RE = re.compile(r"(\d+)\s*(?:/|x|×|pk|pack)\s*(\d+(?:[.,]\d+)?)\s*(ml|l|lt|ltr|cl)?", re.I)


def parse_size_ml(text: str | None) -> Optional[int]:
    """'750 mL' -> 750, '1.75L' -> 1750, '12/750ML' -> 750, '750' -> 750."""
    if not text:
        return None
    t = text.strip()
    pack = _PACK_RE.search(t)
    if pack:
        return _to_ml(pack.group(2), pack.group(3) or ("l" if float(pack.group(2).replace(",", ".")) < 5 else "ml"))
    m = _SIZE_RE.search(t)
    if m:
        return _to_ml(m.group(1), m.group(2))
    bare = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", t)
    if bare:
        v = float(bare.group(1))
        return int(round(v * 1000)) if v < 5 else int(round(v))
    return None


def parse_pack(text: str | None) -> Optional[int]:
    """'12/750ML' -> 12, '6 x 1.75L' -> 6."""
    if not text:
        return None
    m = _PACK_RE.search(text)
    return int(m.group(1)) if m else None


def _to_ml(num: str, unit: str) -> int:
    v = float(num.replace(",", "."))
    u = unit.lower()
    if u.startswith("l"):
        v *= 1000
    elif u == "cl":
        v *= 10
    elif u == "oz":
        v *= 29.5735
    return snap_size(int(round(v)))


STANDARD_SIZES = [50, 100, 200, 355, 375, 700, 750, 1000, 1750]


def snap_size(ml: int) -> int:
    """Snap near-standard volumes (e.g. 1774 from 60oz) to the standard bottle size."""
    best = min(STANDARD_SIZES, key=lambda s: abs(s - ml))
    return best if abs(best - ml) <= best * 0.04 else ml


def size_label(ml: Optional[int]) -> str:
    if not ml:
        return "?"
    if ml >= 1000:
        v = ml / 1000
        return f"{v:g}L"
    return f"{ml}ml"


def tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"'s\b", "s", text)
    parts = re.findall(r"[a-z0-9]+", text)
    return [p for p in parts if p not in STOPWORDS]


def _similarity(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    overlap = len(sa & sb) / len(sa)   # how much of what we READ is explained by the SKU
    reverse = len(sa & sb) / len(sb)   # how much of the SKU name we actually saw
    seq = SequenceMatcher(None, " ".join(sorted(sa)), " ".join(sorted(sb))).ratio()
    return 0.5 * overlap + 0.2 * reverse + 0.3 * seq


@dataclass
class Match:
    sku_id: Optional[int]
    score: float
    candidates: list[dict]  # [{sku_id, score, label}]


class CatalogIndex:
    def __init__(self, skus: Iterable[dict]):
        self.skus = {s["id"]: s for s in skus}
        self.by_upc: dict[str, int] = {}
        self.by_case_upc: dict[str, int] = {}
        self.token_index: dict[str, set[int]] = defaultdict(set)
        self.brand_tokens: dict[int, list[str]] = {}
        self.full_tokens: dict[int, list[str]] = {}
        for s in self.skus.values():
            if s.get("upc"):
                self.by_upc[_digits(s["upc"])] = s["id"]
            if s.get("case_upc"):
                self.by_case_upc[_digits(s["case_upc"])] = s["id"]
            bt = tokens(s["brand"])
            ft = tokens(f"{s['brand']} {s['name']}")
            self.brand_tokens[s["id"]] = bt
            self.full_tokens[s["id"]] = ft
            for t in ft:
                self.token_index[t].add(s["id"])

    def label(self, sku_id: int) -> str:
        s = self.skus[sku_id]
        return f"{s['brand']} {s['name']} {size_label(s['size_ml'])}"

    def match(self, brand: str, product: str, size_ml: Optional[int], upc: Optional[str] = None) -> Match:
        if upc:
            d = _digits(upc)
            for table in (self.by_upc, self.by_case_upc):
                for key in (d, d.lstrip("0")):
                    hit = next((sid for code, sid in table.items() if code == key or code.lstrip("0") == key), None)
                    if hit:
                        return Match(hit, 1.0, [{"sku_id": hit, "score": 1.0, "label": self.label(hit)}])

        read = tokens(f"{brand} {product}")
        read_brand = tokens(brand)
        pool: set[int] = set()
        for t in read:
            pool |= self.token_index.get(t, set())
        if not pool:
            return Match(None, 0.0, [])

        scored: list[tuple[float, int]] = []
        for sid in pool:
            s = self.skus[sid]
            name_score = _similarity(read, self.full_tokens[sid])
            brand_score = _similarity(read_brand, self.brand_tokens[sid]) if read_brand else 0.5
            score = 0.7 * name_score + 0.3 * brand_score
            if size_ml and s.get("size_ml"):
                score = score * (1.0 if s["size_ml"] == size_ml else 0.55)
            scored.append((round(min(score, 0.99), 3), sid))
        scored.sort(key=lambda x: (-x[0], x[1]))

        candidates = [
            {"sku_id": sid, "score": sc, "label": self.label(sid)} for sc, sid in scored[:5] if sc >= MIN_CANDIDATE
        ]
        if not candidates:
            return Match(None, 0.0, [])
        best_score, best_sid = scored[0]
        review_cap = AUTO_ACCEPT - 0.01
        # Near-identical runner-up (same brand, different variant/size) -> ambiguous.
        if len(scored) > 1 and scored[1][0] >= best_score - AMBIGUITY_GAP:
            best_score = min(best_score, review_cap)
        # Size unreadable: the product may be right but the size is a guess.
        if not size_ml:
            best_score = min(best_score, review_cap)
        # Only the brand was read ("Crown Royal") but the brand has several products.
        variant = [t for t in read if t not in set(read_brand)]
        if not variant and len({self.skus[sid]["name"] for _, sid in scored}) > 1:
            best_score = min(best_score, review_cap)
        if best_score < ASSIGN_MIN:
            return Match(None, best_score, candidates)
        return Match(best_sid, best_score, candidates)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")
