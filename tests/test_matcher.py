import csv
from pathlib import Path

import pytest

from backend.app.matcher import AUTO_ACCEPT, CatalogIndex, parse_pack, parse_size_ml, size_label

CATALOG = Path(__file__).resolve().parents[1] / "data" / "sample_catalog.csv"


@pytest.fixture(scope="module")
def index():
    skus = []
    for i, r in enumerate(csv.DictReader(CATALOG.open()), 1):
        skus.append({**r, "id": i, "size_ml": int(r["size_ml"]), "case_pack": int(r["case_pack"])})
    return CatalogIndex(skus)


@pytest.mark.parametrize("text,ml", [
    ("750 mL", 750), ("750ml", 750), ("1.75L", 1750), ("1.75 L", 1750), ("1L", 1000), ("1 Liter", 1000),
    ("50ML", 50), ("375", 375), ("1.75", 1750), ("12/750ML", 750), ("6 x 1.75L", 1750), ("70cl", 700),
    ("200 x 50ML", 50), ("", None), ("Reposado", None),
])
def test_parse_size(text, ml):
    assert parse_size_ml(text) == ml


def test_parse_pack():
    assert parse_pack("12/750ML") == 12
    assert parse_pack("6 x 1.75L") == 6
    assert parse_pack("750 mL") is None


def test_size_label():
    assert size_label(1750) == "1.75L"
    assert size_label(1000) == "1L"
    assert size_label(750) == "750ml"


def test_exact_name_and_size(index):
    m = index.match("Tito's", "Handmade Vodka", 1750)
    assert index.label(m.sku_id) == "Tito's Handmade Vodka 1.75L"
    assert m.score >= AUTO_ACCEPT


def test_size_separates_skus(index):
    a = index.match("Tito's", "Handmade Vodka", 750).sku_id
    b = index.match("Tito's", "Handmade Vodka", 1000).sku_id
    assert a != b


def test_upc_wins(index):
    m = index.match("garbled", "text", None, upc="000000100013")
    assert index.label(m.sku_id) == "Tito's Handmade Vodka 1.75L" and m.score == 1.0


def test_case_upc_matches(index):
    m = index.match("", "", None, upc="10000000100124")
    assert index.label(m.sku_id) == "Jack Daniel's Old No. 7 Tennessee Whiskey 750ml"


def test_unknown_size_goes_to_review(index):
    m = index.match("Jack Daniel's", "Tennessee Honey", None)
    assert m.sku_id is not None and m.score < AUTO_ACCEPT


def test_brand_only_is_ambiguous(index):
    m = index.match("Crown Royal", "", 750)
    assert m.score < AUTO_ACCEPT
    assert len(m.candidates) >= 2


def test_product_not_in_catalog_stays_unknown(index):
    m = index.match("Belvedere", "Vodka", 750)
    assert m.sku_id is None
    assert m.candidates  # still offers suggestions


def test_abbreviated_label_still_matches(index):
    m = index.match("Smirnoff", "No. 21", 750)
    assert index.label(m.sku_id) == "Smirnoff No. 21 Vodka 750ml"
