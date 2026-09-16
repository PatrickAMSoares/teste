"""Testes da detecção de fotos com problema."""

from __future__ import annotations

from conftest import exif_bytes, make_photo
from PIL import ImageEnhance, ImageFilter

from photodedupe.core import badphotos, imaging
from photodedupe.core.exif import read_exif
from photodedupe.core.models import BadFlag


def _flags(path):
    decoded = imaging.decode(path)
    lap = imaging.laplacian_variance(decoded.work_gray)
    flags, detalhes = badphotos.analyze(decoded, read_exif(path), str(path), lap_var=lap)
    return set(flags), detalhes


def test_foto_boa_nao_recebe_marcas(tmp_path):
    path = tmp_path / "boa.jpg"
    make_photo(3, 1600, 1200).save(path, quality=93, exif=exif_bytes())
    flags, _ = _flags(path)
    assert flags == set()


def test_detecta_desfoque(tmp_path):
    path = tmp_path / "borrada.jpg"
    make_photo(4, 1200, 900).filter(ImageFilter.GaussianBlur(8)).save(path, quality=92)
    flags, detalhes = _flags(path)
    assert BadFlag.BLURRY.value in flags
    assert detalhes["nitidez"] < 40


def test_detecta_foto_escura_e_estourada(tmp_path):
    escura = tmp_path / "escura.jpg"
    ImageEnhance.Brightness(make_photo(5, 900, 700)).enhance(0.1).save(escura, quality=92)
    clara = tmp_path / "clara.jpg"
    ImageEnhance.Brightness(make_photo(5, 900, 700)).enhance(3.2).save(clara, quality=92)

    assert BadFlag.DARK.value in _flags(escura)[0]
    assert BadFlag.BRIGHT.value in _flags(clara)[0]


def test_detecta_imagem_minuscula(tmp_path):
    path = tmp_path / "mini.jpg"
    make_photo(6, 200, 150).save(path, quality=85)
    assert BadFlag.TINY.value in _flags(path)[0]


def test_detecta_baixa_resolucao(tmp_path):
    path = tmp_path / "pequena.jpg"
    make_photo(7, 640, 480).save(path, quality=85)
    flags, _ = _flags(path)
    assert BadFlag.LOW_RESOLUTION.value in flags


def test_arquivo_corrompido_e_marcado(tmp_path):
    path = tmp_path / "cortada.jpg"
    make_photo(8, 900, 700).save(path, quality=90)
    dados = path.read_bytes()
    path.write_bytes(dados[: int(len(dados) * 0.55)])   # trunca o arquivo

    from photodedupe.core.analyzer import analyze_path

    resultado = analyze_path(str(path))
    assert "corrupted" in resultado.bad_flags


def test_captura_de_tela_e_reconhecida(tmp_path):
    from PIL import Image, ImageDraw

    path = tmp_path / "Screenshot_2024-05-01.png"
    imagem = Image.new("RGB", (1920, 1080), (245, 246, 248))
    draw = ImageDraw.Draw(imagem)
    draw.rectangle([0, 0, 1920, 60], fill=(30, 40, 60))
    for linha in range(12):
        draw.rectangle([80, 140 + linha * 60, 1400, 170 + linha * 60], fill=(210, 214, 220))
    imagem.save(path)

    flags, detalhes = _flags(path)
    assert BadFlag.SCREENSHOT.value in flags
    assert detalhes["indicio_print"] >= 0.6


def test_rotulos_em_portugues():
    rotulos = badphotos.describe([BadFlag.BLURRY.value, BadFlag.DARK.value])
    assert rotulos == ["Desfocada", "Muito escura"]
