"""Hashes de arquivo e hashes perceptuais.

* :func:`sha256_file` - identidade byte-a-byte (nível 1 da detecção).
* :func:`ahash`, :func:`dhash`, :func:`phash`, :func:`whash` - assinaturas do
  *conteúdo visual* (nível 2). Todas retornam inteiros de 64 bits.

As quatro assinaturas são complementares: pHash resiste a reescala e mudanças
de brilho, dHash captura o gradiente local, aHash é sensível ao tom médio e
wHash (wavelet de Haar) resiste bem a compressão forte. A decisão final nunca
usa um hash isolado - veja :mod:`photodedupe.core.similarity`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

HASH_BITS = 64
_READ_CHUNK = 1 << 20  # 1 MiB


# --------------------------------------------------------------------- SHA-256
def sha256_file(path: str | Path, chunk: int = _READ_CHUNK) -> str:
    """SHA-256 do arquivo inteiro, lido em blocos (memória constante)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def quick_signature(path: str | Path, size: int) -> str:
    """Assinatura rápida (início + meio + fim) para pré-filtrar arquivos grandes.

    Dois arquivos com assinaturas diferentes certamente não são idênticos, o que
    evita ler gigabytes só para descobrir isso. Nunca é usada sozinha para
    declarar duplicata: o SHA-256 completo confirma.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    span = 65536
    with open(path, "rb") as fh:
        h.update(fh.read(span))
        if size > span * 3:
            fh.seek(size // 2)
            h.update(fh.read(span))
            fh.seek(max(0, size - span))
            h.update(fh.read(span))
    return h.hexdigest()


# ------------------------------------------------------------- hashes perceptuais
def _bits_to_int(bits: np.ndarray) -> int:
    """Converte uma matriz booleana 8x8 em inteiro de 64 bits."""
    flat = bits.reshape(-1).astype(np.uint8)
    packed = np.packbits(flat)
    return int.from_bytes(packed.tobytes(), "big")


def _box_resize(gray: np.ndarray, side: int) -> np.ndarray:
    """Média por blocos (sem dependências externas); requer múltiplo exato."""
    h, w = gray.shape
    if (h, w) == (side, side):
        return gray.astype(np.float32)
    fh, fw = h // side, w // side
    if fh >= 1 and fw >= 1 and fh * side == h and fw * side == w:
        return gray.reshape(side, fh, side, fw).mean(axis=(1, 3)).astype(np.float32)
    ys = np.linspace(0, h - 1, side)
    xs = np.linspace(0, w - 1, side)
    yi = np.clip(np.round(ys).astype(int), 0, h - 1)
    xi = np.clip(np.round(xs).astype(int), 0, w - 1)
    return gray[np.ix_(yi, xi)].astype(np.float32)


def ahash(gray32: np.ndarray) -> int:
    """Average hash: 8x8 comparado com a média global."""
    small = _box_resize(gray32, 8)
    return _bits_to_int(small > small.mean())


def dhash(gray32: np.ndarray) -> int:
    """Difference hash: compara cada pixel com o vizinho à direita (9x8)."""
    h, w = gray32.shape
    ys = np.clip(np.round(np.linspace(0, h - 1, 8)).astype(int), 0, h - 1)
    xs = np.clip(np.round(np.linspace(0, w - 1, 9)).astype(int), 0, w - 1)
    small = gray32[np.ix_(ys, xs)].astype(np.float32)
    return _bits_to_int(small[:, 1:] > small[:, :-1])


_DCT_CACHE: dict[int, np.ndarray] = {}


def _dct_matrix(n: int) -> np.ndarray:
    """Matriz da DCT-II ortonormal (equivale a ``scipy.fft.dct(norm='ortho')``)."""
    m = _DCT_CACHE.get(n)
    if m is None:
        k = np.arange(n).reshape(-1, 1)
        i = np.arange(n).reshape(1, -1)
        m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
        m[0, :] /= np.sqrt(2.0)
        _DCT_CACHE[n] = m.astype(np.float32)
    return m


def phash(gray32: np.ndarray) -> int:
    """Perceptual hash por DCT: baixa frequência 8x8, limiar na mediana."""
    g = gray32 if gray32.shape == (32, 32) else _box_resize(gray32, 32)
    m = _dct_matrix(32)
    coeffs = m @ g.astype(np.float32) @ m.T
    low = coeffs[:8, :8].copy()
    dc = low[0, 0]
    low[0, 0] = 0.0
    med = float(np.median(low))
    bits = low > med
    bits[0, 0] = dc > med  # mantém o bit DC informativo em vez de zerá-lo
    return _bits_to_int(bits)


def _haar_step(a: np.ndarray) -> np.ndarray:
    """Um nível da transformada de Haar 2D (in-place sobre uma cópia)."""
    n = a.shape[0]
    half = n // 2
    tmp = np.empty_like(a)
    tmp[:half, :] = (a[0::2, :] + a[1::2, :]) / np.sqrt(2.0)
    tmp[half:, :] = (a[0::2, :] - a[1::2, :]) / np.sqrt(2.0)
    out = np.empty_like(a)
    out[:, :half] = (tmp[:, 0::2] + tmp[:, 1::2]) / np.sqrt(2.0)
    out[:, half:] = (tmp[:, 0::2] - tmp[:, 1::2]) / np.sqrt(2.0)
    return out


def whash(gray32: np.ndarray) -> int:
    """Wavelet hash (Haar, 2 níveis): resistente a compressão agressiva."""
    g = gray32 if gray32.shape == (32, 32) else _box_resize(gray32, 32)
    a = g.astype(np.float32) / 255.0
    a = _haar_step(a)          # 32 -> LL 16x16
    a = _haar_step(a[:16, :16])  # 16 -> LL 8x8
    ll = a[:8, :8].copy()
    med = float(np.median(ll[1:, 1:]))
    return _bits_to_int(ll > med)


def color_signature(rgb_desc: np.ndarray) -> int:
    """Assinatura de cor 64 bits: 4x4 células x 4 bits de matiz/saturação.

    Serve como índice adicional de candidatos para fotos que sofreram recorte
    (onde os hashes de luminância degradam mais rápido).
    """
    arr = rgb_desc.astype(np.float32)
    h, w, _ = arr.shape
    ch, cw = h // 4, w // 4
    cells = arr[: ch * 4, : cw * 4].reshape(4, ch, 4, cw, 3).mean(axis=(1, 3))
    r, g, b = cells[:, :, 0], cells[:, :, 1], cells[:, :, 2]
    rg = np.clip((r - g) / 255.0 * 2.0 + 0.5, 0, 0.999)
    yb = np.clip((0.5 * (r + g) - b) / 255.0 * 2.0 + 0.5, 0, 0.999)
    nib = (rg * 4).astype(np.uint8) * 4 + (yb * 4).astype(np.uint8)
    value = 0
    for v in nib.reshape(-1):
        value = (value << 4) | int(v & 0xF)
    return value & ((1 << 64) - 1)


# ---------------------------------------------------------------------- distância
def hamming(a: int, b: int) -> int:
    """Distância de Hamming entre dois hashes de 64 bits."""
    return int(a ^ b).bit_count()


def hamming_array(value: int, others: np.ndarray) -> np.ndarray:
    """Distância de Hamming de um hash contra um vetor de hashes (uint64)."""
    x = np.bitwise_xor(others.astype(np.uint64), np.uint64(value))
    # popcount vetorizado via tabela de 16 bits
    table = _POPCOUNT16
    out = np.zeros(x.shape, dtype=np.uint8)
    for shift in (0, 16, 32, 48):
        out += table[((x >> np.uint64(shift)) & np.uint64(0xFFFF)).astype(np.uint16)]
    return out


_POPCOUNT16 = np.array([bin(i).count("1") for i in range(1 << 16)], dtype=np.uint8)


def to_signed(value: int) -> int:
    """Converte um hash de 64 bits sem sinal para o inteiro com sinal do SQLite."""
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed(value: int) -> int:
    """Inverso de :func:`to_signed`."""
    return value + (1 << 64) if value < 0 else value


def bands(value: int, n_bands: int = 4) -> list[int]:
    """Divide o hash em faixas para indexação (*multi-index hashing*).

    Com 4 faixas de 16 bits, dois hashes a distância <= 3 compartilham
    obrigatoriamente pelo menos uma faixa idêntica (princípio da casa dos
    pombos), o que permite gerar candidatos sem comparar todos contra todos.
    """
    width = 64 // n_bands
    mask = (1 << width) - 1
    return [(value >> (i * width)) & mask for i in range(n_bands)]
