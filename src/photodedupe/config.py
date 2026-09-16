"""Configurações do aplicativo, persistidas em JSON dentro do perfil do usuário."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .paths import app_home, default_quarantine_dir

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

# Extensões reconhecidas como fotografia. RAW só é lido quando `rawpy` está disponível.
RASTER_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".ppm", ".pgm", ".tga", ".ico",
}
RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf",
    ".rw2", ".raf", ".pef", ".raw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f",
}
ALL_EXTENSIONS = RASTER_EXTENSIONS | RAW_EXTENSIONS


@dataclass
class Thresholds:
    """Limites de similaridade (em %) usados na classificação dos grupos."""

    duplicate_min: float = 95.0      # 95 a 99,9 -> duplicata visual provável
    very_similar_min: float = 85.0   # 85 a 94,9 -> muito semelhante
    similar_min: float = 75.0        # 75 a 84,9 -> semelhante (não duplicata)

    def normalized(self) -> Thresholds:
        dup = min(max(self.duplicate_min, 50.0), 100.0)
        very = min(max(self.very_similar_min, 40.0), dup - 0.1)
        sim = min(max(self.similar_min, 30.0), very - 0.1)
        return Thresholds(round(dup, 2), round(very, 2), round(sim, 2))


@dataclass
class Settings:
    """Todas as preferências do usuário."""

    # --- pastas e varredura ---
    folders: list[str] = field(default_factory=list)
    recursive: bool = True
    follow_symlinks: bool = False
    skip_hidden: bool = True
    min_file_size_kb: int = 8
    min_dimension: int = 64
    include_raw: bool = True
    excluded_folder_names: list[str] = field(
        default_factory=lambda: [
            "$RECYCLE.BIN", "System Volume Information", ".git", "node_modules",
            "AppData", "Windows", "Program Files", "Program Files (x86)",
        ]
    )

    # --- detecção ---
    thresholds: Thresholds = field(default_factory=Thresholds)
    use_embeddings: bool = True          # descritor visual local (nível 3)
    strict_mode: bool = True             # regras anti-falso-positivo (fotos em sequência)
    detect_crops: bool = True
    onnx_model_path: str = ""            # opcional: modelo local ONNX fornecido pelo usuário
    max_candidates_per_file: int = 400

    # --- qualidade / fotos ruins ---
    analyze_bad_photos: bool = True
    blur_threshold: float = 32.0         # score de nitidez (0-100) abaixo disso = desfocada
    dark_threshold: float = 42.0         # luminância média 0-255
    bright_threshold: float = 214.0
    small_photo_max_dim: int = 320

    # --- desempenho ---
    workers: int = 0                     # 0 = automático
    batch_size: int = 64
    thumbnail_size: int = 320
    analysis_long_side: int = 1024       # decodificação reduzida para análise
    store_gray_signature: bool = True
    use_gpu_if_available: bool = False   # só usado com modelo ONNX

    # --- segurança / arquivos ---
    deletion_mode: str = "quarantine"    # "quarantine" | "trash"
    quarantine_dir: str = str(default_quarantine_dir())
    keep_folder_structure_in_quarantine: bool = True
    require_confirmation: bool = True    # sempre verdadeiro na prática; exposto para auditoria
    export_plan_before_action: bool = True

    # --- privacidade ---
    allow_external_services: bool = False   # nunca ativado por padrão
    show_gps_in_ui: bool = False

    # --- interface ---
    theme: str = "dark"                  # "dark" | "light"
    show_technical_details: bool = False
    language: str = "pt_BR"
    window_geometry: str = ""

    # ------------------------------------------------------------------ IO
    @classmethod
    def path(cls) -> Path:
        return app_home() / CONFIG_FILE

    @classmethod
    def load(cls) -> Settings:
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - config corrompida não pode quebrar o app
            log.exception("Falha ao ler configuração; usando padrões")
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> Settings:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        thr = data.pop("thresholds", None)
        obj = cls(**data)
        if isinstance(thr, dict):
            obj.thresholds = Thresholds(
                duplicate_min=float(thr.get("duplicate_min", 95.0)),
                very_similar_min=float(thr.get("very_similar_min", 85.0)),
                similar_min=float(thr.get("similar_min", 75.0)),
            )
        obj.thresholds = obj.thresholds.normalized()
        return obj

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # -------------------------------------------------------------- helpers
    def effective_workers(self) -> int:
        if self.workers and self.workers > 0:
            return self.workers
        cpu = os.cpu_count() or 4
        return max(1, min(cpu - 1, 12)) if cpu > 2 else 1

    def allowed_extensions(self) -> set[str]:
        return set(ALL_EXTENSIONS) if self.include_raw else set(RASTER_EXTENSIONS)

    def quarantine_path(self) -> Path:
        return Path(self.quarantine_dir).expanduser() if self.quarantine_dir else default_quarantine_dir()
