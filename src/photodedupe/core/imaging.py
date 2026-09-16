"""Decodificação de imagens e extração dos dados usados pela análise.

Regras importantes:

* Cada arquivo é decodificado **uma única vez**; todos os sinais (hashes,
  descritor, nitidez, miniatura) derivam do mesmo buffer em memória.
* A decodificação é feita em resolução reduzida (``draft`` do JPEG quando
  possível), o que evita carregar fotos de 50 MP inteiras na RAM.
* Nenhuma função deste módulo grava sobre o arquivo original. A única escrita
  possível é a miniatura, sempre dentro do cache do aplicativo.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- back-ends opcionais
try:  # HEIC/HEIF (fotos de iPhone)
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except Exception:  # noqa: BLE001
    HAS_HEIF = False

try:  # AVIF em versões recentes do pillow-heif
    import pillow_heif  # type: ignore

    if hasattr(pillow_heif, "register_avif_opener"):
        pillow_heif.register_avif_opener()
except Exception:  # noqa: BLE001
    pass

try:  # RAW de câmeras
    import rawpy  # type: ignore

    HAS_RAWPY = True
except Exception:  # noqa: BLE001
    HAS_RAWPY = False

try:  # apenas acelera operações; há fallback puro NumPy para tudo
    import cv2  # type: ignore

    HAS_CV2 = True
    cv2.setNumThreads(1)  # o paralelismo é nosso, por processo
except Exception:  # noqa: BLE001
    HAS_CV2 = False

# Limite de proteção contra "decompression bombs" (PIL aborta acima disso).
Image.MAX_IMAGE_PIXELS = 512_000_000

RAW_SUFFIXES = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf",
    ".rw2", ".raf", ".pef", ".raw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f",
}

HASH_GRID = 32          # base para aHash/dHash/pHash/wHash
DESC_GRID = 64          # base para o descritor visual
MID_GRID = 256          # base para estatísticas de cor (paleta, prints, memes)
SHARPNESS_SIDE = 1024   # lado longo usado na medida de nitidez


class ImageLoadError(Exception):
    """Falha irrecuperável ao ler uma imagem."""


@dataclass
class DecodedImage:
    """Resultado da decodificação: tudo que a análise precisa, em pouca memória."""

    path: str
    width: int                      # dimensões REAIS do arquivo (antes da redução)
    height: int
    format: str
    mode: str
    gray_hash: np.ndarray           # 32x32 float32, 0-255
    gray_desc: np.ndarray           # 64x64 float32, 0-255
    rgb_desc: np.ndarray            # 64x64x3 uint8
    rgb_mid: np.ndarray             # 256x256x3 uint8 (estatísticas de cor/paleta)
    work_gray: np.ndarray           # lado longo <= 1024, float32 (nitidez/exposição)
    quantization: dict | None = None
    truncated: bool = False
    n_frames: int = 1
    icc_profile: bool = False
    extras: dict = field(default_factory=dict)

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000.0

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


# --------------------------------------------------------------------------- leitura
def _open_raw(path: Path, long_side: int) -> tuple[Image.Image, int, int]:
    """Lê um arquivo RAW. Usa a miniatura embutida quando ela é suficiente."""
    if not HAS_RAWPY:
        raise ImageLoadError("Suporte a RAW indisponível (instale 'rawpy')")
    with rawpy.imread(str(path)) as raw:  # type: ignore[union-attr]
        sizes = raw.sizes
        full_w, full_h = int(sizes.width), int(sizes.height)
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:  # type: ignore[attr-defined]
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
                if max(img.size) >= long_side:
                    return img.convert("RGB"), full_w, full_h
            elif thumb.format == rawpy.ThumbFormat.BITMAP:  # type: ignore[attr-defined]
                img = Image.fromarray(thumb.data)
                if max(img.size) >= long_side:
                    return img.convert("RGB"), full_w, full_h
        except Exception:  # noqa: BLE001 - nem todo RAW tem miniatura utilizável
            pass
        rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
        return Image.fromarray(rgb), full_w, full_h


def _open_pil(path: Path, long_side: int) -> tuple[Image.Image, int, int, bool]:
    """Abre com Pillow usando ``draft`` para decodificar já reduzido (JPEG)."""
    truncated = False
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        img = Image.open(path)
        full_w, full_h = img.size
        try:
            img.draft("RGB", (long_side, long_side))
        except Exception:  # noqa: BLE001 - formatos sem suporte a draft
            pass
        img.load()
    except OSError:
        # Segunda tentativa tolerando arquivos truncados/corrompidos.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            img = Image.open(path)
            full_w, full_h = img.size
            try:
                img.draft("RGB", (long_side, long_side))
            except Exception:  # noqa: BLE001
                pass
            img.load()
            truncated = True
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
    return img, full_w, full_h, truncated


def decode(path: str | Path, long_side: int = SHARPNESS_SIDE, thumb_size: int = 0) -> DecodedImage:
    """Decodifica o arquivo e produz todos os arrays de análise.

    Quando ``thumb_size`` é maior que zero, a miniatura JPEG (respeitando a
    proporção original) é gerada a partir do mesmo buffer, em ``extras["thumb"]``,
    evitando uma segunda decodificação do arquivo.

    Levanta :class:`ImageLoadError` quando o arquivo não é uma imagem legível.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    quantization: dict | None = None
    n_frames = 1
    icc = False

    try:
        if suffix in RAW_SUFFIXES:
            img, full_w, full_h = _open_raw(p, long_side)
            truncated = False
            fmt = suffix.lstrip(".").upper()
        else:
            img, full_w, full_h, truncated = _open_pil(p, long_side)
            fmt = (img.format or suffix.lstrip(".").upper() or "DESCONHECIDO").upper()
            quantization = getattr(img, "quantization", None) or None
            n_frames = int(getattr(img, "n_frames", 1) or 1)
            icc = bool(img.info.get("icc_profile"))
    except ImageLoadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImageLoadError(f"{type(exc).__name__}: {exc}") from exc

    if full_w <= 0 or full_h <= 0:
        raise ImageLoadError("Dimensões inválidas")

    mode = img.mode
    try:
        img = ImageOps.exif_transpose(img) or img
    except Exception:  # noqa: BLE001 - EXIF inválido não deve travar a análise
        pass

    # A rotação EXIF também vale para as dimensões reais informadas ao usuário.
    if img.size != (0, 0) and full_w and full_h:
        decoded_landscape = img.size[0] >= img.size[1]
        full_landscape = full_w >= full_h
        if decoded_landscape != full_landscape:
            full_w, full_h = full_h, full_w

    rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    work = _fit(rgb, long_side)
    work_gray = _to_gray_array(work)

    gray_hash = _resize_gray(work_gray, HASH_GRID)
    gray_desc = _resize_gray(work_gray, DESC_GRID)
    rgb_full = rgb.convert("RGB")
    rgb_desc = np.asarray(
        rgb_full.resize((DESC_GRID, DESC_GRID), Image.Resampling.BILINEAR), dtype=np.uint8
    )
    mid_side = min(MID_GRID, max(rgb_full.size))
    rgb_mid = np.asarray(
        rgb_full.resize((mid_side, mid_side), Image.Resampling.NEAREST), dtype=np.uint8
    )

    extras: dict = {}
    if thumb_size > 0:
        try:
            thumb = rgb_full.copy()
            thumb.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            thumb.save(buf, format="JPEG", quality=82, optimize=True)
            extras["thumb"] = buf.getvalue()
            thumb.close()
        except Exception:  # noqa: BLE001 - miniatura é um extra, nunca crítica
            pass

    try:
        img.close()
    except Exception:  # noqa: BLE001
        pass

    return DecodedImage(
        path=str(p),
        width=int(full_w),
        height=int(full_h),
        format=fmt,
        mode=mode,
        gray_hash=gray_hash,
        gray_desc=gray_desc,
        rgb_desc=rgb_desc,
        rgb_mid=rgb_mid,
        work_gray=work_gray,
        quantization=quantization,
        truncated=truncated,
        n_frames=n_frames,
        icc_profile=icc,
        extras=extras,
    )


# --------------------------------------------------------------------- utilitários
def _fit(img: Image.Image, long_side: int) -> Image.Image:
    w, h = img.size
    m = max(w, h)
    if m <= long_side:
        return img
    scale = long_side / float(m)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)


def _to_gray_array(img: Image.Image) -> np.ndarray:
    g = img.convert("L") if img.mode != "L" else img
    return np.asarray(g, dtype=np.float32)


def _resize_gray(arr: np.ndarray, side: int) -> np.ndarray:
    """Redimensiona um array de luminância com média de área (anti-aliasing)."""
    if arr.shape == (side, side):
        return arr.astype(np.float32, copy=True)
    if HAS_CV2:
        interp = cv2.INTER_AREA if arr.shape[0] > side else cv2.INTER_LINEAR
        return cv2.resize(arr, (side, side), interpolation=interp).astype(np.float32)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="L")
    return np.asarray(img.resize((side, side), Image.Resampling.LANCZOS), dtype=np.float32)


_LAPLACIAN = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)


def laplacian_variance(gray: np.ndarray) -> float:
    """Variância do Laplaciano: medida clássica de nitidez.

    Calculada sobre a imagem normalizada para lado longo de 1024 px, de modo que
    fotos de resoluções diferentes possam ser comparadas entre si.
    """
    if gray.size < 64:
        return 0.0
    if HAS_CV2:
        lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    else:
        lap = _convolve2d(gray, _LAPLACIAN)
    return float(np.var(lap))


def _convolve2d(arr: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolução 3x3 sem SciPy (usada quando OpenCV não está disponível)."""
    kh, kw = kernel.shape
    ph, pw = kh // 2, kw // 2
    padded = np.pad(arr, ((ph, ph), (pw, pw)), mode="reflect")
    out = np.zeros_like(arr, dtype=np.float32)
    for i in range(kh):
        for j in range(kw):
            k = kernel[i, j]
            if k:
                out += k * padded[i : i + arr.shape[0], j : j + arr.shape[1]]
    return out


CROP_RATIO = 0.82   # recorte central usado para reconhecer fotos recortadas


def center_crop_resize(arr: np.ndarray, ratio: float = CROP_RATIO) -> np.ndarray:
    """Recorta o centro da imagem e volta ao tamanho original (2D ou 3D).

    É assim que o aplicativo enxerga uma foto "como se" ela tivesse sido
    recortada: comparando a versão recortada de uma com a versão inteira da
    outra, um recorte deixa de passar despercebido.
    """
    h, w = arr.shape[0], arr.shape[1]
    kh, kw = max(8, int(round(h * ratio))), max(8, int(round(w * ratio)))
    oy, ox = (h - kh) // 2, (w - kw) // 2
    crop = arr[oy : oy + kh, ox : ox + kw]
    yi = np.clip(np.round(np.linspace(0, kh - 1, h)).astype(int), 0, kh - 1)
    xi = np.clip(np.round(np.linspace(0, kw - 1, w)).astype(int), 0, kw - 1)
    return crop[np.ix_(yi, xi)] if crop.ndim == 2 else crop[np.ix_(yi, xi)][:, :, :]


def gradient_energy(gray: np.ndarray) -> float:
    """Energia média de bordas - usada para saber se a imagem tem textura suficiente."""
    if gray.size < 16:
        return 0.0
    gx = np.abs(np.diff(gray, axis=1)).mean()
    gy = np.abs(np.diff(gray, axis=0)).mean()
    return float((gx + gy) / 2.0)


def blockiness(gray: np.ndarray) -> float:
    """Mede artefatos de blocos 8x8 típicos de JPEG muito comprimido.

    Compara a diferença média nas fronteiras dos blocos com a diferença média
    dentro dos blocos. Valores acima de ~1.25 indicam compressão agressiva.
    """
    h, w = gray.shape
    if h < 32 or w < 32:
        return 1.0
    diff = np.abs(np.diff(gray, axis=1))
    cols = np.arange(diff.shape[1])
    on_edge = (cols % 8) == 7
    if on_edge.sum() == 0 or (~on_edge).sum() == 0:
        return 1.0
    edge_mean = float(diff[:, on_edge].mean())
    inner_mean = float(diff[:, ~on_edge].mean())
    if inner_mean <= 0.01:
        return 1.0
    return edge_mean / inner_mean


def exposure_stats(gray: np.ndarray) -> dict[str, float]:
    """Estatísticas de exposição usadas na detecção de fotos ruins."""
    flat = gray.reshape(-1)
    if flat.size == 0:
        return {"mean": 0.0, "std": 0.0, "dark_ratio": 1.0, "bright_ratio": 0.0, "contrast": 0.0}
    mean = float(flat.mean())
    std = float(flat.std())
    dark_ratio = float((flat < 24).mean())
    bright_ratio = float((flat > 236).mean())
    p5, p95 = np.percentile(flat, [5, 95])
    return {
        "mean": mean,
        "std": std,
        "dark_ratio": dark_ratio,
        "bright_ratio": bright_ratio,
        "contrast": float(p95 - p5),
    }


def colorfulness(rgb: np.ndarray) -> float:
    """Métrica de Hasler-Süsstrunk: quão colorida é a imagem (0 = cinza)."""
    if rgb.ndim != 3:
        return 0.0
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    rg = np.abs(r - g)
    yb = np.abs(0.5 * (r + g) - b)
    return float(math.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * math.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def unique_color_ratio(rgb: np.ndarray) -> float:
    """Proporção de cores distintas (quantizadas) - baixa em prints e memes."""
    if rgb.ndim != 3 or rgb.size == 0:
        return 0.0
    q = (rgb.astype(np.uint16) >> 3).astype(np.uint16)  # 32 níveis por canal
    packed = (q[:, :, 0] << 10) | (q[:, :, 1] << 5) | q[:, :, 2]
    return float(np.unique(packed).size) / float(packed.size)


def flat_area_ratio(gray: np.ndarray) -> float:
    """Proporção de blocos 8x8 praticamente uniformes (fundo liso de print/meme)."""
    h, w = gray.shape
    bh, bw = h // 8, w // 8
    if bh == 0 or bw == 0:
        return 0.0
    cropped = gray[: bh * 8, : bw * 8].reshape(bh, 8, bw, 8)
    stds = cropped.std(axis=(1, 3))
    return float((stds < 2.0).mean())


# ------------------------------------------------------------------- miniaturas
def make_thumbnail_bytes(path: str | Path, size: int = 320, quality: int = 82) -> bytes | None:
    """Gera a miniatura JPEG de uma foto. Retorna ``None`` em caso de falha."""
    try:
        p = Path(path)
        if p.suffix.lower() in RAW_SUFFIXES:
            img, _, _ = _open_raw(p, size)
        else:
            img, _, _, _ = _open_pil(p, size * 2)
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:  # noqa: BLE001
            pass
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        img.close()
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.debug("Miniatura falhou para %s: %s", path, exc)
        return None


def thumbnail_from_decoded(decoded: DecodedImage, size: int = 320) -> bytes | None:
    """Miniatura barata a partir do buffer já decodificado (sem reler o arquivo)."""
    try:
        arr = decoded.rgb_desc
        img = Image.fromarray(arr, mode="RGB").resize((size, size), Image.Resampling.BICUBIC)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def open_for_view(path: str | Path, max_side: int = 2400) -> Image.Image:
    """Abre uma foto para exibição na interface (comparação, zoom)."""
    p = Path(path)
    if p.suffix.lower() in RAW_SUFFIXES:
        img, _, _ = _open_raw(p, max_side)
    else:
        img, _, _, _ = _open_pil(p, max_side)
    try:
        img = ImageOps.exif_transpose(img) or img
    except Exception:  # noqa: BLE001
        pass
    return _fit(img.convert("RGB"), max_side)


def supported_formats() -> dict[str, bool]:
    """Relatório de back-ends disponíveis, exibido na tela de configurações."""
    return {
        "JPEG/PNG/WEBP/TIFF/BMP/GIF": True,
        "HEIC/HEIF (iPhone)": HAS_HEIF,
        "RAW (CR2/NEF/ARW/DNG...)": HAS_RAWPY,
        "Aceleração OpenCV": HAS_CV2,
    }
