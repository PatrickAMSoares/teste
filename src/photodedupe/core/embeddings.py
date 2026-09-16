"""Nível 3 da detecção: descritor visual (embedding) calculado localmente.

O descritor padrão (:class:`LocalDescriptorProvider`) não depende de rede nem de
modelos baixados: combina um histograma de orientações de gradiente por células
(estilo HOG) com estatísticas de cor oponente. Essa combinação é praticamente
invariante a:

* reescala e reamostragem;
* recompressão JPEG (inclusive qualidade baixa);
* conversão entre formatos;
* mudanças moderadas de brilho/contraste (a normalização por bloco remove ganho);
* recortes pequenos (as células periféricas mudam, as centrais não).

Quem quiser usar uma rede neural no lugar pode apontar, nas configurações, um
arquivo ONNX local (:class:`OnnxEmbeddingProvider`). Nada é enviado para fora do
computador em nenhum dos dois casos.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DESCRIPTOR_VERSION = 2
GRID = 4            # 4x4 células
ORIENTATIONS = 8    # 8 direções por célula
HOG_DIMS = GRID * GRID * ORIENTATIONS   # 128
COLOR_DIMS = GRID * GRID * 2            # 32
DESCRIPTOR_DIMS = HOG_DIMS + COLOR_DIMS  # 160

_HOG_WEIGHT = 0.78
_COLOR_WEIGHT = 0.22


class EmbeddingProvider:
    """Interface comum dos geradores de descritor."""

    name = "base"
    dims = DESCRIPTOR_DIMS

    def compute(self, gray_desc: np.ndarray, rgb_desc: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class LocalDescriptorProvider(EmbeddingProvider):
    """Descritor artesanal, rápido (~0,1 ms por foto) e 100% offline."""

    name = "local-hog-color-v2"
    dims = DESCRIPTOR_DIMS

    def compute(self, gray_desc: np.ndarray, rgb_desc: np.ndarray) -> np.ndarray:
        hog = _hog_descriptor(gray_desc)
        color = _color_descriptor(rgb_desc)
        vec = np.concatenate([hog * _HOG_WEIGHT, color * _COLOR_WEIGHT]).astype(np.float32)
        return _l2(vec)


def _hog_descriptor(gray: np.ndarray) -> np.ndarray:
    """Histograma de orientações de gradiente, normalizado por célula."""
    g = gray.astype(np.float32)
    gy, gx = np.gradient(g)
    mag = np.sqrt(gx * gx + gy * gy)
    ang = np.arctan2(gy, gx)  # -pi..pi
    # orientação sem sinal (0..pi) -> invariante a inversão de contraste
    ang = np.mod(ang, np.pi)
    bin_idx = np.minimum((ang / np.pi * ORIENTATIONS).astype(np.int32), ORIENTATIONS - 1)

    h, w = g.shape
    ch, cw = h // GRID, w // GRID
    out = np.zeros((GRID, GRID, ORIENTATIONS), dtype=np.float32)
    for cy in range(GRID):
        for cx in range(GRID):
            m = mag[cy * ch : (cy + 1) * ch, cx * cw : (cx + 1) * cw].reshape(-1)
            b = bin_idx[cy * ch : (cy + 1) * ch, cx * cw : (cx + 1) * cw].reshape(-1)
            hist = np.bincount(b, weights=m, minlength=ORIENTATIONS).astype(np.float32)
            norm = np.linalg.norm(hist)
            out[cy, cx] = hist / norm if norm > 1e-6 else hist
    return out.reshape(-1)


def _color_descriptor(rgb: np.ndarray) -> np.ndarray:
    """Média dos canais oponentes (r-g, y-b) por célula, sem o brilho absoluto."""
    arr = rgb.astype(np.float32)
    h, w, _ = arr.shape
    ch, cw = h // GRID, w // GRID
    cells = arr[: ch * GRID, : cw * GRID].reshape(GRID, ch, GRID, cw, 3).mean(axis=(1, 3))
    r, g, b = cells[:, :, 0], cells[:, :, 1], cells[:, :, 2]
    rg = (r - g) / 255.0
    yb = (0.5 * (r + g) - b) / 255.0
    vec = np.stack([rg, yb], axis=-1).reshape(-1).astype(np.float32)
    # remove o viés global de cor (balanço de branco levemente diferente)
    vec = vec - vec.mean()
    return _l2(vec)


def _l2(vec: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(vec))
    return (vec / n).astype(np.float32) if n > 1e-8 else vec.astype(np.float32)


class OnnxEmbeddingProvider(EmbeddingProvider):
    """Usa um modelo ONNX local fornecido pelo usuário (opcional).

    O modelo precisa aceitar um tensor NCHW float32 e devolver um vetor por
    imagem. O arquivo permanece no computador; nenhuma imagem sai da máquina.
    """

    name = "onnx-local"

    def __init__(self, model_path: str | Path, input_side: int = 224, use_gpu: bool = False) -> None:
        import onnxruntime as ort  # importado só quando o recurso é ativado

        providers = ["CPUExecutionProvider"]
        if use_gpu:
            available = set(ort.get_available_providers())
            for candidate in ("DmlExecutionProvider", "CUDAExecutionProvider"):
                if candidate in available:
                    providers.insert(0, candidate)
                    break
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
        self._input = self._session.get_inputs()[0].name
        self._side = input_side
        self._lock = threading.Lock()
        out_shape = self._session.get_outputs()[0].shape
        self.dims = int(out_shape[-1]) if isinstance(out_shape[-1], int) else DESCRIPTOR_DIMS

    def compute(self, gray_desc: np.ndarray, rgb_desc: np.ndarray) -> np.ndarray:
        from PIL import Image

        img = Image.fromarray(rgb_desc).resize((self._side, self._side), Image.Resampling.BILINEAR)
        arr = np.asarray(img, dtype=np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        arr = (arr - mean) / std
        tensor = np.transpose(arr, (2, 0, 1))[None, ...].astype(np.float32)
        with self._lock:
            out = self._session.run(None, {self._input: tensor})[0]
        return _l2(np.asarray(out, dtype=np.float32).reshape(-1))


_provider_cache: dict[str, EmbeddingProvider] = {}


def get_provider(onnx_model_path: str = "", use_gpu: bool = False) -> EmbeddingProvider:
    """Devolve o gerador de descritor configurado, com fallback seguro."""
    key = f"{onnx_model_path}|{use_gpu}"
    provider = _provider_cache.get(key)
    if provider is not None:
        return provider
    if onnx_model_path and Path(onnx_model_path).exists():
        try:
            provider = OnnxEmbeddingProvider(onnx_model_path, use_gpu=use_gpu)
            log.info("Descritor visual: modelo ONNX local %s", onnx_model_path)
        except Exception:  # noqa: BLE001 - qualquer falha volta ao descritor interno
            log.exception("Falha ao carregar modelo ONNX; usando descritor interno")
            provider = LocalDescriptorProvider()
    else:
        provider = LocalDescriptorProvider()
    _provider_cache[key] = provider
    return provider


# ----------------------------------------------------------------- serialização
def pack(vec: np.ndarray) -> bytes:
    """Serializa o descritor em float16 (metade do espaço, precisão suficiente)."""
    return np.asarray(vec, dtype=np.float16).tobytes()


def unpack(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Similaridade do cosseno entre descritores (0..1 na prática)."""
    if a is None or b is None or a.size == 0 or a.shape != b.shape:
        return float("nan")
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-8 or nb < 1e-8:
        return float("nan")
    return float(np.dot(a, b) / (na * nb))


def random_hyperplanes(dims: int, bits: int, seed: int = 20240501) -> np.ndarray:
    """Hiperplanos determinísticos para LSH sobre descritores."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=(bits, dims)).astype(np.float32)


def lsh_key(vec: np.ndarray, planes: np.ndarray) -> int:
    """Chave LSH: sinais das projeções, empacotados em um inteiro."""
    proj = planes @ vec.astype(np.float32)
    bits = (proj > 0).astype(np.uint8)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value
