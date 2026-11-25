import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKDECK_DATA", str(tmp_path))
    monkeypatch.setenv("STOCKDECK_RECOGNIZER", "mock")
    from fastapi.testclient import TestClient
    from backend.app.main import app

    with TestClient(app) as c:
        yield c


def shelf_jpeg() -> bytes:
    img = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(img)
    for y in (140, 290, 440, 590):
        d.rectangle([0, y, 800, y + 8], fill="saddlebrown")
    for x in range(20, 780, 60):
        d.rectangle([x, 40, x + 40, 135], fill="steelblue")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture()
def photo() -> bytes:
    return shelf_jpeg()
