"""Índice de qualidade explicável (0 a 100).

O índice **nunca** se baseia só no tamanho do arquivo. Ele combina sete
componentes, cada um com peso fixo e valor próprio de 0 a 100:

=====================  ====  ============================================
Componente             Peso  O que mede
=====================  ====  ============================================
Resolução                30  megapixels reais do arquivo
Nitidez                  22  variância do Laplaciano (normalizada por escala)
Compressão               18  bits por pixel, tabelas JPEG e artefatos de bloco
Formato                  10  RAW/TIFF > PNG/HEIC > WEBP > JPEG
Metadados (EXIF)          8  quanto do EXIF original sobreviveu
Tamanho do arquivo        7  bytes por megapixel (indicador secundário)
Originalidade             5  indícios de ser o arquivo original, não uma cópia
=====================  ====  ============================================

Cada componente também gera uma frase em português explicando o resultado, que
a interface mostra ao lado da foto.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from .exif import ExifData
from .imaging import DecodedImage, blockiness, laplacian_variance
from .models import QualityReport

WEIGHTS = {
    "resolucao": 30.0,
    "nitidez": 22.0,
    "compressao": 18.0,
    "formato": 10.0,
    "exif": 8.0,
    "tamanho": 7.0,
    "originalidade": 5.0,
}

FORMAT_SCORES = {
    "CR2": 100, "CR3": 100, "NEF": 100, "ARW": 100, "DNG": 100, "ORF": 100,
    "RW2": 100, "RAF": 100, "PEF": 100, "RAW": 100, "SR2": 100, "X3F": 100,
    "TIFF": 95, "TIF": 95,
    "PNG": 88,
    "HEIC": 82, "HEIF": 82, "AVIF": 82,
    "WEBP": 78,
    "JPEG": 70, "JPG": 70, "JPE": 70, "MPO": 68,
    "BMP": 66, "TGA": 60, "PPM": 60,
    "GIF": 40, "ICO": 30,
}

LOSSLESS_FORMATS = {"PNG", "TIFF", "TIF", "BMP", "PPM", "PGM", "TGA"}
RAW_FORMATS = {"CR2", "CR3", "NEF", "ARW", "DNG", "ORF", "RW2", "RAF", "PEF", "RAW", "SR2", "X3F", "NRW", "MRW"}

# Marcas típicas de cópia/reexportação no nome do arquivo.
_COPY_PATTERNS = [
    (re.compile(r"\(\s*\d+\s*\)\s*$"), "nome com sufixo de cópia, por exemplo “(1)”"),
    (re.compile(r"[-_ ](c[óo]pia|copy|copia)\b", re.I), "nome indica cópia"),
    (re.compile(r"\bc[óo]pia de\b", re.I), "nome indica cópia"),
    (re.compile(r"[-_](min|small|low|compressed|comprimid[ao]|reduzid[ao]|web|thumb)\b", re.I), "nome sugere versão reduzida"),
    (re.compile(r"[-_ ](edit(ed|ado)?|final|v\d+|retoque)\b", re.I), "nome sugere versão editada"),
    (re.compile(r"\b(whatsapp|wa\d{4}|img-\d{8}-wa\d+)\b", re.I), "arquivo recebido por mensageiro (recomprimido)"),
    (re.compile(r"\b(screenshot|captura de tela|screen shot|print)\b", re.I), "parece captura de tela"),
]

# Tabela de quantização padrão de luminância (JPEG Anexo K).
_STD_LUMA_Q = np.array(
    [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
     14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
     18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
     49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99],
    dtype=np.float64,
)


def _interp(value: float, points: list[tuple[float, float]]) -> float:
    """Interpolação linear em uma curva de ancoragem."""
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + t * (y1 - y0)
    return points[-1][1]


# ------------------------------------------------------------------ componentes
def resolution_score(width: int, height: int) -> float:
    mp = (width * height) / 1_000_000.0
    if mp <= 0:
        return 0.0
    return _interp(
        math.log10(max(mp, 0.01)),
        [
            (math.log10(0.05), 2.0),
            (math.log10(0.3), 20.0),
            (math.log10(1.0), 42.0),
            (math.log10(2.0), 56.0),
            (math.log10(6.0), 76.0),
            (math.log10(12.0), 88.0),
            (math.log10(24.0), 96.0),
            (math.log10(50.0), 100.0),
        ],
    )


def sharpness_score(lap_var: float) -> float:
    """Converte a variância do Laplaciano em nota 0-100."""
    return _interp(
        math.log10(max(lap_var, 0.1)),
        [
            (math.log10(1.0), 0.0),
            (math.log10(10.0), 8.0),
            (math.log10(50.0), 30.0),
            (math.log10(150.0), 55.0),
            (math.log10(400.0), 75.0),
            (math.log10(1000.0), 90.0),
            (math.log10(3000.0), 100.0),
        ],
    )


def sharpness_label(score: float) -> str:
    if score >= 80:
        return "excelente"
    if score >= 60:
        return "boa"
    if score >= 40:
        return "regular"
    if score >= 22:
        return "baixa"
    return "muito baixa (possível desfoque)"


def estimate_jpeg_quality(quantization: dict | None) -> int | None:
    """Estima a qualidade usada na gravação JPEG a partir das tabelas de quantização."""
    if not quantization:
        return None
    table = quantization.get(0)
    if table is None:
        return None
    arr = np.asarray(list(table), dtype=np.float64)[:64]
    if arr.size < 64 or not np.any(arr):
        return None
    ratios = arr / _STD_LUMA_Q[: arr.size]
    scale = float(np.median(ratios)) * 100.0
    if scale <= 0:
        return None
    # Inversão da relação usada pelos codificadores JPEG:
    #   qualidade >= 50 -> escala = 200 - 2*qualidade  (escala <= 100)
    #   qualidade <  50 -> escala = 5000 / qualidade   (escala >  100)
    quality = (200.0 - scale) / 2.0 if scale <= 100.0 else 5000.0 / scale
    return int(max(1, min(100, round(quality))))


def compression_score(
    fmt: str, size_bytes: int, width: int, height: int, decoded: DecodedImage | None
) -> tuple[float, float, str, int | None]:
    """Nota de compressão + bits por pixel + rótulo + qualidade JPEG estimada."""
    pixels = max(1, width * height)
    bpp = (size_bytes * 8.0) / pixels
    fmt = fmt.upper()
    jpeg_q = estimate_jpeg_quality(decoded.quantization if decoded else None)

    if fmt in RAW_FORMATS:
        return 100.0, bpp, "nenhuma (RAW)", None
    if fmt in LOSSLESS_FORMATS:
        base = _interp(bpp, [(1.0, 80.0), (4.0, 92.0), (8.0, 98.0), (16.0, 100.0)])
        return base, bpp, "sem perdas", None

    base = _interp(bpp, [(0.05, 5.0), (0.15, 20.0), (0.35, 42.0), (0.7, 62.0), (1.2, 78.0), (2.5, 92.0), (5.0, 100.0)])
    if jpeg_q is not None:
        q_score = _interp(float(jpeg_q), [(30.0, 10.0), (50.0, 32.0), (70.0, 55.0), (85.0, 78.0), (92.0, 92.0), (100.0, 100.0)])
        base = 0.45 * base + 0.55 * q_score
    if decoded is not None:
        block = blockiness(decoded.work_gray)
        if block > 1.15:
            penalty = min(25.0, (block - 1.15) * 90.0)
            base = max(0.0, base - penalty)

    if base >= 85:
        label = "muito baixa"
    elif base >= 65:
        label = "baixa"
    elif base >= 45:
        label = "moderada"
    elif base >= 25:
        label = "alta"
    else:
        label = "muito alta (artefatos visíveis)"
    return base, bpp, label, jpeg_q


def format_score(fmt: str) -> float:
    return float(FORMAT_SCORES.get(fmt.upper(), 65))


def exif_score(exif: ExifData) -> float:
    if not exif.present:
        return 0.0
    score = 100.0 * exif.completeness()
    if exif.has_camera:
        score = min(100.0, score + 8.0)
    return score


def filesize_score(size_bytes: int, width: int, height: int) -> float:
    """Bytes por megapixel - indicador secundário de densidade de informação."""
    mp = max(0.01, (width * height) / 1_000_000.0)
    kb_per_mp = (size_bytes / 1024.0) / mp
    return _interp(
        math.log10(max(kb_per_mp, 1.0)),
        [
            (math.log10(30.0), 5.0),
            (math.log10(120.0), 28.0),
            (math.log10(350.0), 55.0),
            (math.log10(900.0), 78.0),
            (math.log10(2500.0), 94.0),
            (math.log10(8000.0), 100.0),
        ],
    )


def originality_score(path: str, fmt: str, exif: ExifData, width: int, height: int) -> tuple[float, list[str]]:
    """0 a 1: chance de este arquivo ser o original, e não uma reexportação."""
    score = 0.5
    notes: list[str] = []
    # Os padrões são testados sobre o nome sem extensão: "IMG_1234 (1).jpg"
    # precisa casar com o sufixo de cópia no fim do nome.
    name = Path(path).stem

    for pattern, note in _COPY_PATTERNS:
        if pattern.search(name):
            score -= 0.22
            notes.append(f"O nome do arquivo indica que é uma cópia ({note}).")
            break

    if fmt.upper() in RAW_FORMATS:
        score += 0.35
        notes.append("Arquivo RAW: é a captura original da câmera.")
    elif fmt.upper() in ("TIFF", "TIF"):
        score += 0.12

    if exif.present and exif.has_camera:
        score += 0.2
        notes.append("Mantém os metadados originais da câmera.")
    elif not exif.present:
        score -= 0.18
        notes.append("Sem EXIF: provavelmente foi reexportada ou passou por um aplicativo que removeu os metadados.")

    if exif.edited_by_software:
        score -= 0.15
        notes.append(f"Gravada por “{exif.software}”, o que sugere reexportação.")

    if exif.exif_image_width and exif.exif_image_height:
        exif_px = exif.exif_image_width * exif.exif_image_height
        real_px = width * height
        if real_px and abs(exif_px - real_px) / max(exif_px, real_px) < 0.02:
            score += 0.18
            notes.append("As dimensões batem com as registradas pela câmera (não foi redimensionada).")
        elif real_px < exif_px * 0.9:
            score -= 0.2
            notes.append("A imagem é menor do que a registrada no EXIF: foi redimensionada depois da captura.")

    return max(0.0, min(1.0, score)), notes


# --------------------------------------------------------------------- agregação
def compute_quality(
    path: str,
    size_bytes: int,
    decoded: DecodedImage | None,
    exif: ExifData,
    *,
    width: int | None = None,
    height: int | None = None,
    fmt: str | None = None,
    lap_var: float | None = None,
) -> QualityReport:
    """Calcula o índice de qualidade completo de uma foto."""
    w = width if width is not None else (decoded.width if decoded else 0)
    h = height if height is not None else (decoded.height if decoded else 0)
    f = (fmt if fmt is not None else (decoded.format if decoded else "")).upper()

    if lap_var is None:
        lap_var = laplacian_variance(decoded.work_gray) if decoded is not None else 0.0

    res = resolution_score(w, h)
    sharp = sharpness_score(lap_var)
    comp, bpp, comp_label, jpeg_q = compression_score(f, size_bytes, w, h, decoded)
    fmt_s = format_score(f)
    exif_s = exif_score(exif)
    size_s = filesize_score(size_bytes, w, h)
    orig, orig_notes = originality_score(path, f, exif, w, h)

    components = {
        "resolucao": res,
        "nitidez": sharp,
        "compressao": comp,
        "formato": fmt_s,
        "exif": exif_s,
        "tamanho": size_s,
        "originalidade": orig * 100.0,
    }
    total = sum(components[k] * WEIGHTS[k] for k in WEIGHTS) / sum(WEIGHTS.values())

    sharp_label = sharpness_label(sharp)
    reasons = [
        f"Resolução: {w} × {h} ({(w * h) / 1_000_000:.1f} MP).",
        f"Nitidez: {sharp_label}.",
        f"Compressão: {comp_label}" + (f" (qualidade JPEG estimada: {jpeg_q}%)." if jpeg_q else "."),
        f"Formato: {f or 'desconhecido'}.",
        ("Metadados EXIF presentes." if exif.present else "Sem metadados EXIF."),
    ]
    reasons.extend(orig_notes)

    return QualityReport(
        score=round(total, 1),
        components=components,
        reasons=reasons,
        sharpness_raw=float(lap_var),
        sharpness_label=sharp_label,
        compression_label=comp_label,
        bits_per_pixel=bpp,
        jpeg_quality_estimate=jpeg_q,
        originality=orig,
    )


def quality_label(score: float) -> str:
    if score >= 85:
        return "Excelente"
    if score >= 70:
        return "Muito boa"
    if score >= 55:
        return "Boa"
    if score >= 40:
        return "Regular"
    if score >= 25:
        return "Baixa"
    return "Muito baixa"
