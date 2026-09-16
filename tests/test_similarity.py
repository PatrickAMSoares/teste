"""Testes da comparação visual e da classificação em categorias."""

from __future__ import annotations

from conftest import exif_bytes, make_photo

from photodedupe.config import Thresholds
from photodedupe.core.analyzer import analyze_path, signature_from_result
from photodedupe.core.models import Category
from photodedupe.core.similarity import classify, compare, quick_reject


def _sig(path, file_id=1):
    return signature_from_result(file_id, analyze_path(str(path)))


def test_arquivo_identico_e_duplicata_exata(tmp_path):
    import shutil

    a = tmp_path / "a.jpg"
    make_photo(1, 800, 600).save(a, quality=92)
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)

    resultado = compare(_sig(a, 1), _sig(b, 2))
    assert resultado.percent == 100.0
    assert resultado.category is Category.EXACT
    assert "SHA-256" in resultado.notes[0]
    assert "idênticos" in resultado.human_phrase()


def test_recompressao_e_reescala_sao_duplicatas_visuais(tmp_path):
    photo = make_photo(11, 1200, 900)
    original = tmp_path / "original.jpg"
    photo.save(original, quality=95, exif=exif_bytes())

    variacoes = {
        "recomprimida": (tmp_path / "v1.jpg", lambda p: photo.save(p, quality=45, exif=exif_bytes())),
        "reduzida": (tmp_path / "v2.jpg", lambda p: photo.resize((600, 450)).save(p, quality=80, exif=exif_bytes())),
        "png": (tmp_path / "v3.png", lambda p: photo.save(p)),
        "webp": (tmp_path / "v4.webp", lambda p: photo.save(p, quality=80)),
    }
    base = _sig(original, 1)
    for nome, (path, salvar) in variacoes.items():
        salvar(path)
        resultado = compare(base, _sig(path, 2))
        assert resultado.percent >= 95.0, f"{nome} ficou em {resultado.percent:.1f}%"
        assert resultado.category is Category.VISUAL


def test_fotos_diferentes_nao_sao_agrupadas(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    make_photo(21, 800, 600).save(a, quality=92)
    make_photo(88, 800, 600).save(b, quality=92)
    resultado = compare(_sig(a, 1), _sig(b, 2))
    assert resultado.percent < 60.0
    assert resultado.category is Category.SIMILAR


def test_fotos_em_sequencia_nao_viram_duplicatas(tmp_path):
    """Duas fotos da mesma cena, tiradas em instantes diferentes."""
    a = tmp_path / "seq1.jpg"
    b = tmp_path / "seq2.jpg"
    make_photo(31, 900, 700).save(a, quality=94, exif=exif_bytes("2024:05:01 10:00:00", "10"))
    make_photo(31, 900, 700, variant=3).save(b, quality=94, exif=exif_bytes("2024:05:01 10:00:02", "55"))

    resultado = compare(_sig(a, 1), _sig(b, 2), strict=True)
    assert resultado.category is not Category.VISUAL
    assert resultado.percent < 95.0
    assert any("momentos diferentes" in nota for nota in resultado.notes)


def test_mesmo_instante_de_captura_reforca_a_duplicata(tmp_path):
    photo = make_photo(33, 900, 700)
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    photo.save(a, quality=95, exif=exif_bytes("2024:05:01 10:00:00", "10"))
    photo.save(b, quality=70, exif=exif_bytes("2024:05:01 10:00:00", "10"))
    resultado = compare(_sig(a, 1), _sig(b, 2), strict=True)
    assert resultado.percent >= 95.0
    assert any("Mesma data/hora" in nota for nota in resultado.notes)


def test_recorte_e_detectado_mas_nao_como_duplicata(tmp_path):
    photo = make_photo(41, 1200, 900)
    original = tmp_path / "inteira.jpg"
    photo.save(original, quality=92, exif=exif_bytes())
    recorte = tmp_path / "recorte.jpg"
    photo.crop((90, 70, 1110, 830)).save(recorte, quality=92, exif=exif_bytes())

    com_recorte = compare(_sig(original, 1), _sig(recorte, 2), detect_crops=True)
    sem_recorte = compare(_sig(original, 1), _sig(recorte, 2), detect_crops=False)

    # A detecção de recorte melhora a pontuação (o centro coincide)...
    assert com_recorte.percent > sem_recorte.percent
    # ...mas o resultado nunca é promovido a duplicata.
    assert com_recorte.category is not Category.VISUAL
    assert com_recorte.percent < 95.0
    assert any("recort" in nota.lower() for nota in com_recorte.notes)


def test_classificacao_respeita_limites_configurados():
    limites = Thresholds(duplicate_min=97.0, very_similar_min=90.0, similar_min=80.0)
    assert classify(100.0, limites) is Category.EXACT
    assert classify(98.0, limites) is Category.VISUAL
    assert classify(95.0, limites) is Category.VERY_SIMILAR
    assert classify(85.0, limites) is Category.SIMILAR


def test_descarte_rapido_nao_elimina_duplicatas(tmp_path):
    photo = make_photo(51, 800, 600)
    a, b = tmp_path / "a.jpg", tmp_path / "b.png"
    photo.save(a, quality=90)
    photo.save(b)
    assert quick_reject(_sig(a, 1), _sig(b, 2)) is False

    # O descarte rápido é só uma otimização: ele nunca pode eliminar uma
    # duplicata, mas deve dispensar pares claramente sem relação.
    c = tmp_path / "c.jpg"
    make_photo(77, 800, 600).save(c, quality=90)
    assert quick_reject(_sig(a, 1), _sig(c, 3)) is True
    assert compare(_sig(a, 1), _sig(c, 3)).percent < 30.0


def test_frases_em_linguagem_simples(tmp_path):
    photo = make_photo(61, 700, 500)
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    photo.save(a, quality=95)
    photo.save(b, quality=60)
    frase = compare(_sig(a, 1), _sig(b, 2)).human_phrase()
    assert "%" in frase and "similaridade" in frase
    assert "hash" not in frase.lower()
