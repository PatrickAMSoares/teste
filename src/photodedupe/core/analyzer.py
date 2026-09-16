"""Análise de um arquivo isolado - função executada nos processos de trabalho.

Tudo aqui é *somente leitura* sobre a foto do usuário. O único efeito colateral
possível é a geração da miniatura, entregue como bytes para quem chamou gravar
no cache do aplicativo.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import numpy as np

from . import badphotos, embeddings, hashing, imaging
from .exif import ExifData, read_exif
from .imaging import ImageLoadError
from .models import AnalysisResult, PhotoSignature
from .quality import compute_quality


@dataclass(frozen=True)
class AnalyzerOptions:
    """Parâmetros da análise (precisa ser picklável: vai para outros processos)."""

    analysis_long_side: int = 1024
    thumbnail_size: int = 320
    compute_thumbnail: bool = True
    use_embeddings: bool = True
    analyze_bad_photos: bool = True
    store_gray_signature: bool = True
    blur_threshold: float = 32.0
    dark_threshold: float = 42.0
    bright_threshold: float = 214.0
    small_photo_max_dim: int = 320
    onnx_model_path: str = ""
    use_gpu: bool = False


def analyze_path(path: str, options: AnalyzerOptions | None = None) -> AnalysisResult:
    """Extrai todos os sinais de um arquivo. Nunca levanta exceção."""
    opts = options or AnalyzerOptions()
    started = time.perf_counter()
    result = AnalysisResult(path=path)
    try:
        stat = os.stat(path)
        result.size = int(stat.st_size)
        result.mtime_ns = int(stat.st_mtime_ns)
    except OSError as exc:
        result.ok = False
        result.error = f"Não foi possível ler o arquivo: {exc.strerror or exc}"
        return result

    try:
        result.sha256 = hashing.sha256_file(path)
    except OSError as exc:
        result.ok = False
        result.error = f"Falha ao calcular o hash: {exc.strerror or exc}"
        return result

    try:
        decoded = imaging.decode(
            path,
            long_side=opts.analysis_long_side,
            thumb_size=opts.thumbnail_size if opts.compute_thumbnail else 0,
        )
    except ImageLoadError as exc:
        result.ok = False
        result.error = f"Imagem ilegível: {exc}"
        result.bad_flags = ["corrupted"]
        return result
    except Exception as exc:  # noqa: BLE001 - robustez acima de tudo no lote
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        result.bad_flags = ["corrupted"]
        return result

    result.width = decoded.width
    result.height = decoded.height
    result.format = decoded.format
    result.mode = decoded.mode

    gray = decoded.gray_hash
    result.phash = hashing.phash(gray)
    result.dhash = hashing.dhash(gray)
    result.ahash = hashing.ahash(gray)
    result.whash = hashing.whash(gray)
    result.color_sig = hashing.color_signature(decoded.rgb_desc)
    result.texture = imaging.gradient_energy(decoded.gray_desc)

    # Assinaturas do recorte central: permitem reconhecer a mesma foto quando
    # uma das versões foi recortada (as bordas mudam, o centro não).
    cropped_gray = imaging.center_crop_resize(gray)
    result.crop_phash = hashing.phash(cropped_gray)
    result.crop_dhash = hashing.dhash(cropped_gray)

    if opts.store_gray_signature:
        result.gray_blob = np.clip(gray, 0, 255).astype(np.uint8).tobytes()

    if opts.use_embeddings:
        provider = embeddings.get_provider(opts.onnx_model_path, opts.use_gpu)
        vector = provider.compute(decoded.gray_desc, decoded.rgb_desc)
        result.desc_blob = embeddings.pack(vector)
        crop_vector = provider.compute(
            imaging.center_crop_resize(decoded.gray_desc),
            imaging.center_crop_resize(decoded.rgb_desc),
        )
        result.crop_desc_blob = embeddings.pack(crop_vector)

    exif: ExifData = read_exif(path)
    result.exif = exif

    lap_var = imaging.laplacian_variance(decoded.work_gray)
    result.quality = compute_quality(path, result.size, decoded, exif, lap_var=lap_var)

    if opts.analyze_bad_photos:
        flags, details = badphotos.analyze(
            decoded,
            exif,
            path,
            lap_var=lap_var,
            blur_threshold=opts.blur_threshold,
            dark_threshold=opts.dark_threshold,
            bright_threshold=opts.bright_threshold,
            small_max_dim=opts.small_photo_max_dim,
        )
        result.bad_flags = flags
        result.bad_details = details
    if decoded.truncated and "corrupted" not in result.bad_flags:
        result.bad_flags.append("corrupted")

    if opts.compute_thumbnail:
        result.thumb_bytes = decoded.extras.get("thumb")

    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return result


def signature_from_result(file_id: int, result: AnalysisResult) -> PhotoSignature:
    """Converte o resultado bruto na assinatura usada pela comparação."""
    gray = (
        np.frombuffer(result.gray_blob, dtype=np.uint8).astype(np.float32).reshape(32, 32)
        if result.gray_blob
        else None
    )
    return PhotoSignature(
        file_id=file_id,
        path=result.path,
        size=result.size,
        width=result.width,
        height=result.height,
        format=result.format,
        sha256=result.sha256,
        phash=result.phash,
        dhash=result.dhash,
        ahash=result.ahash,
        whash=result.whash,
        color_sig=result.color_sig,
        crop_phash=result.crop_phash,
        crop_dhash=result.crop_dhash,
        gray=gray,
        descriptor=embeddings.unpack(result.desc_blob),
        crop_descriptor=embeddings.unpack(result.crop_desc_blob),
        quality=result.quality.score,
        texture=result.texture,
        taken_at=result.exif.taken_at,
        capture_key=result.exif.capture_key(),
        camera=result.exif.camera_label(),
    )
