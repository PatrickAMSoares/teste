"""Comparação visual entre duas fotos (níveis 2 e 3 da detecção).

Nenhum sinal isolado decide. A similaridade final combina três famílias
independentes de evidência:

1. **Hashes perceptuais** (pHash, dHash, wHash, aHash) - robustos a reescala e
   recompressão, mas fracos em imagens com pouca textura.
2. **Descritor visual** (HOG + cor oponente) - robusto a recorte e a mudanças
   de brilho/contraste.
3. **Correlação estrutural** (NCC sobre a miniatura em tons de cinza,
   normalizada em média e desvio) - confirma que o conteúdo é o mesmo pixel a
   pixel, independentemente de ganho de brilho.

Sobre o resultado são aplicadas regras conservadoras que **reduzem** a
similaridade quando há indício de que se trata de fotos diferentes (por
exemplo, instantes de captura distintos em uma sequência/rajada). O objetivo é
manter alta a detecção de duplicatas reais e baixo o número de falsos
positivos.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .embeddings import cosine
from .hashing import hamming
from .models import Category, PhotoSignature

# --- constantes de calibração (ajustadas no benchmark: tools/benchmark.py) ---
HASH_CUTOFF = 22.0          # distância de Hamming a partir da qual o hash não informa nada
HASH_WEIGHTS = {"phash": 0.36, "dhash": 0.30, "whash": 0.20, "ahash": 0.14}
COS_FLOOR, COS_CEIL = 0.840, 0.9975
NCC_FLOOR, NCC_CEIL = 0.560, 0.9970
W_HASH, W_DESC, W_NCC = 0.30, 0.28, 0.42

LOW_TEXTURE = 2.5           # energia de gradiente abaixo disso = imagem "lisa"
ASPECT_TOLERANCE = 0.012    # 1,2% de diferença de proporção ainda é a mesma foto
CROP_CAP = 94.5             # recortes ficam, no máximo, em "muito semelhante"
DIFFERENT_CAPTURE_CAP = 91.0
DIFFERENT_CAMERA_CAP = 93.5
LOW_TEXTURE_CAP = 96.0


@dataclass
class SimilarityResult:
    """Resultado da comparação entre duas fotos."""

    percent: float
    category: Category
    hash_sim: float = 0.0
    desc_sim: float = 0.0
    ncc_sim: float = 0.0
    distances: dict[str, int] = field(default_factory=dict)
    cosine: float = 0.0
    ncc: float = 0.0
    notes: list[str] = field(default_factory=list)
    capped_by: str = ""

    def to_dict(self) -> dict:
        return {
            "percent": round(self.percent, 2),
            "category": self.category.value,
            "hash_sim": round(self.hash_sim, 4),
            "desc_sim": round(self.desc_sim, 4),
            "ncc_sim": round(self.ncc_sim, 4),
            "distances": self.distances,
            "cosine": round(self.cosine, 4),
            "ncc": round(self.ncc, 4),
            "notes": self.notes,
            "capped_by": self.capped_by,
        }

    def human_phrase(self) -> str:
        """Explicação em linguagem simples (requisito de UX)."""
        p = f"{self.percent:.1f}".replace(".", ",")
        if self.category is Category.EXACT:
            return "Arquivos idênticos — mesmo conteúdo byte a byte."
        if self.percent >= 99.0:
            return f"É a mesma foto, salva de outra forma — {p}% de similaridade."
        if self.percent >= 95.0:
            return f"Muito provavelmente são a mesma foto — {p}% de similaridade."
        if self.percent >= 85.0:
            return f"Imagens quase iguais, com diferenças perceptíveis — {p}% de similaridade."
        return f"Imagens parecidas, provavelmente fotos diferentes — {p}% de similaridade."


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _norm(value: float, floor: float, ceil: float) -> float:
    if ceil <= floor:
        return 0.0
    return _clamp01((value - floor) / (ceil - floor))


def _as_gray(sig: PhotoSignature) -> np.ndarray | None:
    g = sig.gray
    if g is None:
        return None
    arr = np.asarray(g, dtype=np.float32)
    if arr.ndim == 1:
        side = int(round(float(arr.size) ** 0.5))
        if side * side != arr.size:
            return None
        arr = arr.reshape(side, side)
    return arr


def _zscore(arr: np.ndarray) -> np.ndarray | None:
    std = float(arr.std())
    if std < 1e-4:
        return None
    return (arr - float(arr.mean())) / std


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    za, zb = _zscore(a), _zscore(b)
    if za is None or zb is None:
        return float("nan")
    return float(np.clip((za * zb).mean(), -1.0, 1.0))


def _center_crop_resized(arr: np.ndarray, ratio: float = 0.82) -> np.ndarray:
    """Recorte central reamostrado para o tamanho original (tolerância a crop)."""
    n = arr.shape[0]
    k = max(8, int(round(n * ratio)))
    off = (n - k) // 2
    crop = arr[off : off + k, off : off + k]
    idx = np.clip(np.round(np.linspace(0, k - 1, n)).astype(int), 0, k - 1)
    return crop[np.ix_(idx, idx)]


def structural_similarity(a: PhotoSignature, b: PhotoSignature, allow_crop: bool = True) -> float:
    """Correlação cruzada normalizada entre as assinaturas 32x32 (com tolerância a recorte)."""
    ga, gb = _as_gray(a), _as_gray(b)
    if ga is None or gb is None or ga.shape != gb.shape:
        return float("nan")
    best = _ncc(ga, gb)
    if allow_crop:
        for variant in (_ncc(_center_crop_resized(ga), gb), _ncc(ga, _center_crop_resized(gb))):
            if not np.isnan(variant) and (np.isnan(best) or variant > best):
                best = variant
    return best


def hash_similarity(a: PhotoSignature, b: PhotoSignature) -> tuple[float, dict[str, int]]:
    """Média ponderada dos quatro hashes perceptuais, normalizada em 0..1."""
    distances = {
        "phash": hamming(a.phash, b.phash),
        "dhash": hamming(a.dhash, b.dhash),
        "whash": hamming(a.whash, b.whash),
        "ahash": hamming(a.ahash, b.ahash),
    }
    total = 0.0
    for name, weight in HASH_WEIGHTS.items():
        total += weight * _clamp01(1.0 - distances[name] / HASH_CUTOFF)
    return total, distances


def compare(
    a: PhotoSignature,
    b: PhotoSignature,
    *,
    thresholds=None,
    strict: bool = True,
    detect_crops: bool = True,
) -> SimilarityResult:
    """Compara duas fotos e devolve similaridade em % + categoria."""
    from ..config import Thresholds  # import tardio evita ciclo

    thr = thresholds or Thresholds()
    notes: list[str] = []

    # ---------------------------------------------------- nível 1: idêntico
    if a.sha256 and a.sha256 == b.sha256:
        return SimilarityResult(
            percent=100.0,
            category=Category.EXACT,
            hash_sim=1.0,
            desc_sim=1.0,
            ncc_sim=1.0,
            distances={"phash": 0, "dhash": 0, "whash": 0, "ahash": 0},
            cosine=1.0,
            ncc=1.0,
            notes=["Os dois arquivos têm exatamente o mesmo conteúdo (SHA-256 idêntico)."],
        )

    # ---------------------------------------- níveis 2 e 3: sinais visuais
    hash_sim, distances = hash_similarity(a, b)
    cos = cosine(a.descriptor, b.descriptor)
    ncc = structural_similarity(a, b, allow_crop=detect_crops)

    desc_sim = _norm(cos, COS_FLOOR, COS_CEIL) if not np.isnan(cos) else float("nan")
    ncc_sim = _norm(ncc, NCC_FLOOR, NCC_CEIL) if not np.isnan(ncc) else float("nan")

    parts: list[tuple[float, float]] = [(hash_sim, W_HASH)]
    if not np.isnan(desc_sim):
        parts.append((desc_sim, W_DESC))
    if not np.isnan(ncc_sim):
        parts.append((ncc_sim, W_NCC))
    weight_sum = sum(w for _, w in parts)
    combined = sum(v * w for v, w in parts) / weight_sum if weight_sum else 0.0
    percent = 100.0 * combined
    capped_by = ""

    # ------------------------------------------------ ajustes conservadores
    # Proporção diferente: quase sempre indica recorte ou outro enquadramento.
    if a.aspect and b.aspect:
        rel = abs(a.aspect - b.aspect) / max(a.aspect, b.aspect)
        if rel > ASPECT_TOLERANCE:
            if percent > CROP_CAP:
                percent = CROP_CAP
                capped_by = "recorte"
            notes.append(
                f"As proporções são diferentes ({_ratio_text(a)} × {_ratio_text(b)}): uma delas provavelmente foi recortada."
            )

    if strict:
        # Instantes de captura distintos = fotos distintas (rajada, sequência).
        if a.capture_key and b.capture_key:
            if a.capture_key != b.capture_key:
                if percent > DIFFERENT_CAPTURE_CAP:
                    percent = DIFFERENT_CAPTURE_CAP
                    capped_by = "instantes de captura diferentes"
                notes.append(
                    "Foram fotografadas em momentos diferentes "
                    f"({a.taken_at.replace('T', ' ')} e {b.taken_at.replace('T', ' ')}): são fotos distintas, ainda que parecidas."
                )
            else:
                notes.append("Mesma data/hora de captura no EXIF: são versões da mesma fotografia.")
                percent = min(99.9, percent + 1.5)

        # Câmeras diferentes com EXIF completo nos dois arquivos.
        if a.camera and b.camera and a.camera != b.camera:
            if percent > DIFFERENT_CAMERA_CAP:
                percent = DIFFERENT_CAMERA_CAP
                capped_by = capped_by or "câmeras diferentes"
            notes.append(f"Registradas por câmeras diferentes ({a.camera} e {b.camera}).")

        # Imagens muito lisas (fundos, digitalizações em branco) enganam hashes.
        if a.texture and b.texture and min(a.texture, b.texture) < LOW_TEXTURE:
            if percent > LOW_TEXTURE_CAP:
                percent = LOW_TEXTURE_CAP
                capped_by = capped_by or "pouca textura"
            notes.append("Imagens com pouca textura: a comparação automática é menos confiável aqui.")

    percent = float(max(0.0, min(99.9, percent)))
    category = classify(percent, thr)

    if not notes:
        notes.append("Conteúdo visual praticamente idêntico nos três métodos de comparação.")

    return SimilarityResult(
        percent=percent,
        category=category,
        hash_sim=hash_sim,
        desc_sim=0.0 if np.isnan(desc_sim) else desc_sim,
        ncc_sim=0.0 if np.isnan(ncc_sim) else ncc_sim,
        distances=distances,
        cosine=0.0 if np.isnan(cos) else cos,
        ncc=0.0 if np.isnan(ncc) else ncc,
        notes=notes,
        capped_by=capped_by,
    )


def _ratio_text(sig: PhotoSignature) -> str:
    return f"{sig.width}×{sig.height}"


def classify(percent: float, thresholds=None) -> Category:
    """Traduz a similaridade em categoria, usando os limites configurados."""
    from ..config import Thresholds

    thr = thresholds or Thresholds()
    if percent >= 100.0:
        return Category.EXACT
    if percent >= thr.duplicate_min:
        return Category.VISUAL
    if percent >= thr.very_similar_min:
        return Category.VERY_SIMILAR
    return Category.SIMILAR


def quick_reject(a: PhotoSignature, b: PhotoSignature, margin: int = 26) -> bool:
    """Descarte barato antes da comparação completa (evita contas desnecessárias)."""
    if a.sha256 and a.sha256 == b.sha256:
        return False
    return hamming(a.phash, b.phash) > margin and hamming(a.dhash, b.dhash) > margin
