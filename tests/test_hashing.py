"""Testes dos hashes de arquivo e perceptuais."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import make_photo

from photodedupe.core import hashing, imaging


def test_sha256_identico_para_mesmo_conteudo(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"conteudo identico" * 1000)
    b.write_bytes(b"conteudo identico" * 1000)
    assert hashing.sha256_file(a) == hashing.sha256_file(b)

    b.write_bytes(b"conteudo diferente" * 1000)
    assert hashing.sha256_file(a) != hashing.sha256_file(b)


def test_assinatura_rapida_difere_com_conteudo(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"x" * 300_000)
    b = tmp_path / "b.bin"
    b.write_bytes(b"x" * 299_999 + b"y")
    assert hashing.quick_signature(a, a.stat().st_size) != hashing.quick_signature(b, b.stat().st_size)


@pytest.mark.parametrize("função", [hashing.ahash, hashing.dhash, hashing.phash, hashing.whash])
def test_hashes_sao_estaveis_e_de_64_bits(função):
    gray = np.asarray(make_photo(5).convert("L").resize((32, 32)), dtype=np.float32)
    valor = função(gray)
    assert 0 <= valor < (1 << 64)
    assert função(gray) == valor  # determinístico


def test_hashes_resistem_a_recompressao_e_reescala(tmp_path):
    photo = make_photo(9, 800, 600)
    original = tmp_path / "o.png"
    photo.save(original)
    comprimida = tmp_path / "c.jpg"
    photo.save(comprimida, quality=45)
    reduzida = tmp_path / "r.jpg"
    photo.resize((400, 300)).save(reduzida, quality=80)
    outra = tmp_path / "x.jpg"
    make_photo(999, 800, 600).save(outra, quality=95)

    def hashes(path):
        decoded = imaging.decode(path)
        return hashing.phash(decoded.gray_hash), hashing.dhash(decoded.gray_hash)

    base_p, base_d = hashes(original)
    for path in (comprimida, reduzida):
        p, d = hashes(path)
        assert hashing.hamming(base_p, p) <= 4
        assert hashing.hamming(base_d, d) <= 6
    p, d = hashes(outra)
    assert hashing.hamming(base_p, p) >= 14


def test_faixas_garantem_candidatos_para_distancias_pequenas():
    valor = 0x0123456789ABCDEF
    proximo = valor ^ 0b111  # distância 3
    faixas_a = hashing.bands(valor)
    faixas_b = hashing.bands(proximo)
    assert any(a == b for a, b in zip(faixas_a, faixas_b))


def test_conversao_para_inteiro_com_sinal_do_sqlite():
    for valor in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        assert hashing.from_signed(hashing.to_signed(valor)) == valor
        assert -(1 << 63) <= hashing.to_signed(valor) < (1 << 63)


def test_hamming_vetorizado_bate_com_o_escalar():
    valores = np.array([0, 1, 0xFFFF, (1 << 64) - 1], dtype=np.uint64)
    referencia = 0x0F0F0F0F0F0F0F0F
    esperado = [hashing.hamming(referencia, int(v)) for v in valores]
    assert list(hashing.hamming_array(referencia, valores)) == esperado
