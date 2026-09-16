"""Fixtures compartilhadas pelos testes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PIL.ExifTags import TAGS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TAG = {v: k for k, v in TAGS.items()}


def make_photo(seed: int = 1, width: int = 640, height: int = 480, variant: int = 0) -> Image.Image:
    """Cria uma imagem sintética com estrutura e textura suficientes para os testes."""
    rng = np.random.default_rng(seed * 1009 + variant)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    canvas = np.zeros((height, width, 3), dtype=np.float32)
    canvas[..., 0] = 128 + 70 * np.sin(xx / (18.0 + seed % 7) + seed)
    canvas[..., 1] = 120 + 60 * np.cos(yy / (22.0 + seed % 5) + seed * 0.7)
    canvas[..., 2] = 140 + 50 * np.sin((xx + yy) / (30.0 + seed % 11))
    for _ in range(4 + seed % 3):
        cx, cy = rng.uniform(0.1, 0.9) * width, rng.uniform(0.1, 0.9) * height
        r = rng.uniform(0.05, 0.2) * width
        mask = ((xx - cx - variant * 6) ** 2 + (yy - cy) ** 2) < r * r
        canvas[mask] = rng.uniform(10, 245, size=3)
    canvas += rng.normal(0, 5, canvas.shape)
    return Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), mode="RGB")


def exif_bytes(when: str = "2024:05:01 10:00:00", subsec: str = "10", model: str = "Canon EOS R6") -> bytes:
    exif = Image.Exif()
    exif[_TAG["Make"]] = "Canon"
    exif[_TAG["Model"]] = model
    sub = exif.get_ifd(0x8769)
    sub[_TAG["DateTimeOriginal"]] = when
    sub[_TAG["SubsecTimeOriginal"]] = subsec
    sub[_TAG["ISOSpeedRatings"]] = 400
    sub[_TAG["FNumber"]] = (28, 10)
    sub[_TAG["ExposureTime"]] = (1, 250)
    sub[_TAG["LensModel"]] = "RF24-70mm F2.8"
    sub[_TAG["FocalLength"]] = (50, 1)
    return exif.tobytes()


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Isola o perfil do aplicativo (banco, miniaturas, configuração) em tmp."""
    home = tmp_path / "perfil"
    home.mkdir()
    monkeypatch.setenv("PHOTODEDUPE_HOME", str(home))
    return home


@pytest.fixture()
def library(tmp_path):
    """Biblioteca pequena com duplicatas conhecidas."""
    import shutil

    folder = tmp_path / "fotos"
    (folder / "sub").mkdir(parents=True)
    photo = make_photo(3, 800, 600)

    original = folder / "IMG_0001.jpg"
    photo.save(original, quality=95, exif=exif_bytes())

    # duplicata exata
    shutil.copy2(original, folder / "sub" / "IMG_0001 (1).jpg")
    # duplicata visual: recompressão forte
    photo.save(folder / "IMG_0001_whats.jpg", quality=40, exif=exif_bytes())
    # duplicata visual: redimensionada e convertida
    photo.resize((400, 300), Image.Resampling.LANCZOS).save(folder / "IMG_0001_web.png")
    # foto diferente
    make_photo(77, 800, 600).save(folder / "IMG_0002.jpg", quality=95, exif=exif_bytes("2024:05:01 11:00:00"))
    # foto em sequência da mesma cena, outro instante
    make_photo(3, 800, 600, variant=2).save(
        folder / "IMG_0001_seq.jpg", quality=95, exif=exif_bytes("2024:05:01 10:00:03", subsec="44")
    )
    return folder


@pytest.fixture()
def db(app_home):
    from photodedupe.db.database import Database

    database = Database()
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    from photodedupe.db.repository import Repository

    return Repository(db)


@pytest.fixture()
def settings(app_home):
    from photodedupe.config import Settings

    cfg = Settings()
    cfg.min_file_size_kb = 0
    cfg.workers = 1
    cfg.quarantine_dir = str(Path(os.environ["PHOTODEDUPE_HOME"]) / "quarentena")
    return cfg
