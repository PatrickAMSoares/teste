"""Detecção opcional de fotos com problema.

Esta análise é **independente** da detecção de duplicatas e nunca provoca
exclusão automática: ela apenas marca fotos para que o usuário possa revisá-las.

Critérios: desfoque, subexposição, superexposição, arquivo corrompido, imagem
minúscula, captura de tela e imagens que não parecem fotografias (memes,
documentos digitalizados, arte gerada por computador).
"""

from __future__ import annotations

import re
from pathlib import Path

from .exif import ExifData
from .imaging import (
    DecodedImage,
    colorfulness,
    exposure_stats,
    flat_area_ratio,
    unique_color_ratio,
)
from .models import BadFlag
from .quality import sharpness_score

# Resoluções de tela completas (largura × altura). Usar o par exato - e não
# cada dimensão isolada - evita confundir uma foto 1920×1440 com um print.
_SCREEN_RESOLUTIONS = {
    (1024, 768), (1280, 720), (1280, 800), (1280, 1024), (1360, 768), (1366, 768),
    (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080), (1920, 1200),
    (2048, 1152), (2240, 1400), (2256, 1504), (2560, 1440), (2560, 1600), (2880, 1620),
    (2880, 1800), (3024, 1964), (3440, 1440), (3456, 2234), (3840, 2160), (5120, 2880),
    # Telas de celular (retrato) - prints de aplicativos.
    (750, 1334), (828, 1792), (1080, 1920), (1080, 2340), (1080, 2400), (1125, 2436),
    (1170, 2532), (1179, 2556), (1242, 2688), (1284, 2778), (1290, 2796), (1440, 2560),
    (1440, 3040), (1440, 3120),
}
_SCREEN_RESOLUTIONS |= {(h, w) for w, h in _SCREEN_RESOLUTIONS}
SCREENSHOT_THRESHOLD = 0.65

_SCREENSHOT_NAME = re.compile(r"(screenshot|screen[ _-]?shot|captura[ _-]?de[ _-]?tela|print[ _-]?screen|scr\d{4})", re.I)
_MEME_NAME = re.compile(r"(meme|whatsapp image|img-\d{8}-wa|sticker|figurinha|wallpaper|papel de parede)", re.I)


def analyze(
    decoded: DecodedImage,
    exif: ExifData,
    path: str,
    *,
    lap_var: float,
    blur_threshold: float = 32.0,
    dark_threshold: float = 42.0,
    bright_threshold: float = 214.0,
    small_max_dim: int = 320,
) -> tuple[list[str], dict]:
    """Retorna ``(flags, detalhes)`` para uma foto já decodificada."""
    flags: list[str] = []
    gray = decoded.work_gray
    stats = exposure_stats(gray)
    flat = flat_area_ratio(gray)
    uniq = unique_color_ratio(decoded.rgb_mid)
    color = colorfulness(decoded.rgb_mid)
    sharp = sharpness_score(lap_var)
    name = Path(path).name
    max_dim = max(decoded.width, decoded.height)
    megapixels = decoded.megapixels

    details = {
        "nitidez": round(sharp, 1),
        "laplaciano": round(lap_var, 2),
        "luminancia_media": round(stats["mean"], 1),
        "contraste": round(stats["contrast"], 1),
        "proporcao_escura": round(stats["dark_ratio"], 3),
        "proporcao_clara": round(stats["bright_ratio"], 3),
        "areas_lisas": round(flat, 3),
        "cores_distintas": round(uniq, 4),
        "colorido": round(color, 1),
    }

    if decoded.truncated:
        flags.append(BadFlag.CORRUPTED.value)

    if max_dim < small_max_dim:
        flags.append(BadFlag.TINY.value)
    elif megapixels < 0.5:
        flags.append(BadFlag.LOW_RESOLUTION.value)

    # Desfoque: só avaliamos quando há resolução suficiente para a medida fazer sentido.
    if max_dim >= 200 and sharp < blur_threshold and flat < 0.7:
        flags.append(BadFlag.BLURRY.value)

    if stats["mean"] < dark_threshold and stats["dark_ratio"] > 0.45:
        flags.append(BadFlag.DARK.value)
    elif stats["mean"] > bright_threshold or stats["bright_ratio"] > 0.38:
        flags.append(BadFlag.BRIGHT.value)

    screenshot = _screenshot_score(decoded, exif, name, flat, uniq, color)
    details["indicio_print"] = round(screenshot, 2)
    if screenshot >= SCREENSHOT_THRESHOLD:
        flags.append(BadFlag.SCREENSHOT.value)

    non_photo = _non_photo_score(exif, flat, uniq, color, name, stats)
    details["indicio_nao_foto"] = round(non_photo, 2)
    if non_photo >= 0.65 and BadFlag.SCREENSHOT.value not in flags:
        flags.append(BadFlag.NON_PHOTO.value)

    return flags, details


def _screenshot_score(
    decoded: DecodedImage, exif: ExifData, name: str, flat: float, uniq: float, color: float
) -> float:
    """Indício de captura de tela, de 0 a 1.

    Cópias de fotos reexportadas (PNG/WEBP sem EXIF) chegavam a ser confundidas
    com prints. Por isso o peso está nos sinais realmente característicos de uma
    tela: resolução de monitor exata, grandes áreas chapadas e paleta reduzida.
    A ausência de EXIF, sozinha, quase não conta.
    """
    score = 0.0
    if _SCREENSHOT_NAME.search(name):
        score += 0.6
    if (decoded.width, decoded.height) in _SCREEN_RESOLUTIONS:
        score += 0.3
    if flat > 0.35:
        score += 0.2
    elif flat > 0.22:
        score += 0.1
    if uniq < 0.01:
        score += 0.15
    elif uniq < 0.02:
        score += 0.05
    if color < 10:
        score += 0.1
    if decoded.format.upper() == "PNG" and not exif.has_camera:
        score += 0.1
    if exif.has_camera:
        score -= 0.6   # foto de câmera: praticamente descarta a hipótese
    return max(0.0, min(1.0, score))


def _non_photo_score(
    exif: ExifData, flat: float, uniq: float, color: float, name: str, stats: dict
) -> float:
    score = 0.0
    if flat > 0.45:
        score += 0.35
    elif flat > 0.3:
        score += 0.18
    if uniq < 0.008:
        score += 0.3
    elif uniq < 0.02:
        score += 0.15
    if color < 8:
        score += 0.15
    if stats["contrast"] > 200 and flat > 0.3:
        score += 0.1  # arte/documento: preto no branco
    if _MEME_NAME.search(name):
        score += 0.25
    if exif.has_camera:
        score -= 0.5
    return max(0.0, min(1.0, score))


def describe(flags: list[str]) -> list[str]:
    """Traduz as marcas técnicas para frases em português."""
    out = []
    for flag in flags:
        try:
            out.append(BadFlag(flag).label)
        except ValueError:
            out.append(flag)
    return out
