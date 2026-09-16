"""Testes de decodificação, métricas de imagem e índice de qualidade."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import exif_bytes, make_photo
from PIL import Image, ImageFilter

from photodedupe.core import imaging, quality
from photodedupe.core.exif import read_exif
from photodedupe.core.imaging import ImageLoadError


def test_decodifica_e_gera_todos_os_arrays(tmp_path):
    path = tmp_path / "foto.jpg"
    make_photo(2, 1600, 1200).save(path, quality=90)
    decoded = imaging.decode(path, thumb_size=256)

    assert (decoded.width, decoded.height) == (1600, 1200)   # dimensões reais
    assert decoded.gray_hash.shape == (32, 32)
    assert decoded.gray_desc.shape == (64, 64)
    assert decoded.rgb_desc.shape == (64, 64, 3)
    assert max(decoded.work_gray.shape) <= 1024              # decodificação reduzida
    assert decoded.format == "JPEG"
    assert decoded.extras["thumb"]
    assert Image.open(__import__("io").BytesIO(decoded.extras["thumb"])).size[0] == 256


def test_arquivo_invalido_gera_erro_tratado(tmp_path):
    ruim = tmp_path / "quebrado.jpg"
    ruim.write_bytes(b"isto nao e uma imagem")
    with pytest.raises(ImageLoadError):
        imaging.decode(ruim)


def test_recorte_central_preserva_formato():
    arr = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
    assert imaging.center_crop_resize(arr).shape == (64, 64)
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    assert imaging.center_crop_resize(rgb).shape == (64, 64, 3)


def test_nitidez_cai_com_desfoque(tmp_path):
    nitida = tmp_path / "nitida.png"
    borrada = tmp_path / "borrada.png"
    photo = make_photo(4, 800, 600)
    photo.save(nitida)
    photo.filter(ImageFilter.GaussianBlur(6)).save(borrada)

    var_nitida = imaging.laplacian_variance(imaging.decode(nitida).work_gray)
    var_borrada = imaging.laplacian_variance(imaging.decode(borrada).work_gray)
    assert var_nitida > var_borrada * 3
    assert quality.sharpness_score(var_nitida) > quality.sharpness_score(var_borrada)


@pytest.mark.parametrize("qualidade_jpeg", [30, 50, 70, 85, 95])
def test_estimativa_de_qualidade_jpeg(tmp_path, qualidade_jpeg):
    path = tmp_path / f"q{qualidade_jpeg}.jpg"
    make_photo(6, 640, 480).save(path, quality=qualidade_jpeg)
    estimada = quality.estimate_jpeg_quality(imaging.decode(path).quantization)
    assert estimada is not None
    assert abs(estimada - qualidade_jpeg) <= 3


def test_indice_de_qualidade_ordena_versoes_da_mesma_foto(tmp_path):
    photo = make_photo(8, 1600, 1200)
    caminhos = {}
    caminhos["original"] = tmp_path / "original.jpg"
    photo.save(caminhos["original"], quality=95, exif=exif_bytes())
    caminhos["media"] = tmp_path / "media.jpg"
    photo.save(caminhos["media"], quality=70)
    caminhos["ruim"] = tmp_path / "ruim.jpg"
    photo.resize((640, 480)).save(caminhos["ruim"], quality=35)

    notas = {}
    for nome, path in caminhos.items():
        decoded = imaging.decode(path)
        notas[nome] = quality.compute_quality(str(path), path.stat().st_size, decoded, read_exif(path)).score

    assert notas["original"] > notas["media"] > notas["ruim"]


def test_relatorio_de_qualidade_explica_em_portugues(tmp_path):
    path = tmp_path / "foto.jpg"
    make_photo(10, 1200, 900).save(path, quality=88, exif=exif_bytes())
    report = quality.compute_quality(str(path), path.stat().st_size, imaging.decode(path), read_exif(path))

    assert 0 <= report.score <= 100
    assert set(report.components) == set(quality.WEIGHTS)
    assert any("Resolução" in motivo for motivo in report.reasons)
    assert report.sharpness_label
    assert quality.quality_label(report.score)


def test_originalidade_penaliza_nome_de_copia(tmp_path):
    from photodedupe.core.exif import ExifData

    nota_original, _ = quality.originality_score("/fotos/IMG_1234.jpg", "JPEG", ExifData(present=True, camera_make="Canon"), 4000, 3000)
    nota_copia, motivos = quality.originality_score("/fotos/IMG_1234 (1).jpg", "JPEG", ExifData(present=True, camera_make="Canon"), 4000, 3000)
    assert nota_copia < nota_original
    assert any("cópia" in m.lower() for m in motivos)


def test_raw_e_lossless_pontuam_mais_que_jpeg():
    assert quality.format_score("CR2") > quality.format_score("TIFF") >= quality.format_score("PNG")
    assert quality.format_score("PNG") > quality.format_score("JPEG")
