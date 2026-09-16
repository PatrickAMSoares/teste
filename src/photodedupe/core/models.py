"""Estruturas de dados compartilhadas entre análise, banco e interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from .exif import ExifData


class Category(str, Enum):
    """Categorias de resultado apresentadas ao usuário."""

    EXACT = "exact"              # 🔴 duplicatas exatas (byte a byte)
    VISUAL = "visual"            # 🟠 duplicatas visuais
    VERY_SIMILAR = "very_similar"  # 🟡 fotos muito semelhantes
    SIMILAR = "similar"          # 🔵 fotos semelhantes

    @property
    def label(self) -> str:
        return {
            Category.EXACT: "Duplicatas exatas",
            Category.VISUAL: "Duplicatas visuais",
            Category.VERY_SIMILAR: "Fotos muito semelhantes",
            Category.SIMILAR: "Fotos semelhantes",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Category.EXACT: "🔴",
            Category.VISUAL: "🟠",
            Category.VERY_SIMILAR: "🟡",
            Category.SIMILAR: "🔵",
        }[self]

    @property
    def color(self) -> str:
        return {
            Category.EXACT: "#e5484d",
            Category.VISUAL: "#f76b15",
            Category.VERY_SIMILAR: "#f5d90a",
            Category.SIMILAR: "#3b9eff",
        }[self]

    @property
    def description(self) -> str:
        return {
            Category.EXACT: "Arquivos tecnicamente idênticos (mesmo conteúdo byte a byte).",
            Category.VISUAL: "A mesma fotografia salva de formas diferentes (tamanho, formato ou compressão).",
            Category.VERY_SIMILAR: "Praticamente a mesma imagem, mas com diferenças que podem ser relevantes.",
            Category.SIMILAR: "Imagens parecidas - provavelmente NÃO são duplicatas. Revise com atenção.",
        }[self]

    @property
    def order(self) -> int:
        return {Category.EXACT: 0, Category.VISUAL: 1, Category.VERY_SIMILAR: 2, Category.SIMILAR: 3}[self]


class FileStatus(str, Enum):
    PENDING = "pending"        # descoberto, ainda não analisado
    ANALYZED = "analyzed"      # analisado com sucesso
    ERROR = "error"            # falha de leitura
    MISSING = "missing"        # sumiu do disco desde a última análise
    REMOVED = "removed"        # movido para quarentena/lixeira pelo usuário


class BadFlag(str, Enum):
    BLURRY = "blurry"
    DARK = "dark"
    BRIGHT = "bright"
    CORRUPTED = "corrupted"
    TINY = "tiny"
    SCREENSHOT = "screenshot"
    NON_PHOTO = "non_photo"
    LOW_RESOLUTION = "low_resolution"

    @property
    def label(self) -> str:
        return {
            BadFlag.BLURRY: "Desfocada",
            BadFlag.DARK: "Muito escura",
            BadFlag.BRIGHT: "Superexposta",
            BadFlag.CORRUPTED: "Arquivo corrompido",
            BadFlag.TINY: "Imagem muito pequena",
            BadFlag.SCREENSHOT: "Captura de tela",
            BadFlag.NON_PHOTO: "Não parece uma fotografia",
            BadFlag.LOW_RESOLUTION: "Baixa resolução",
        }[self]


@dataclass
class QualityReport:
    """Índice de qualidade explicável (0 a 100) de uma foto."""

    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    sharpness_raw: float = 0.0
    sharpness_label: str = ""
    compression_label: str = ""
    bits_per_pixel: float = 0.0
    jpeg_quality_estimate: int | None = None
    originality: float = 0.0

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "reasons": self.reasons,
            "sharpness_raw": round(self.sharpness_raw, 2),
            "sharpness_label": self.sharpness_label,
            "compression_label": self.compression_label,
            "bits_per_pixel": round(self.bits_per_pixel, 3),
            "jpeg_quality_estimate": self.jpeg_quality_estimate,
            "originality": round(self.originality, 3),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> QualityReport:
        data = data or {}
        return cls(
            score=float(data.get("score", 0.0)),
            components={k: float(v) for k, v in (data.get("components") or {}).items()},
            reasons=list(data.get("reasons") or []),
            sharpness_raw=float(data.get("sharpness_raw", 0.0)),
            sharpness_label=data.get("sharpness_label", ""),
            compression_label=data.get("compression_label", ""),
            bits_per_pixel=float(data.get("bits_per_pixel", 0.0)),
            jpeg_quality_estimate=data.get("jpeg_quality_estimate"),
            originality=float(data.get("originality", 0.0)),
        )


@dataclass
class PhotoSignature:
    """Tudo que a comparação visual precisa de uma foto (leve, cabe em memória)."""

    file_id: int
    path: str
    size: int = 0
    width: int = 0
    height: int = 0
    format: str = ""
    sha256: str = ""
    phash: int = 0
    dhash: int = 0
    ahash: int = 0
    whash: int = 0
    color_sig: int = 0
    crop_phash: int = 0                 # hashes do recorte central (detecta recortes)
    crop_dhash: int = 0
    gray: np.ndarray | None = None      # 32x32 uint8
    descriptor: np.ndarray | None = None
    crop_descriptor: np.ndarray | None = None
    quality: float = 0.0
    texture: float = 0.0                # energia de bordas (confiabilidade dos hashes)
    taken_at: str = ""
    capture_key: str = ""
    camera: str = ""
    thumb: str = ""
    bad_flags: str = ""

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000.0

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        return str(Path(self.path).parent)


@dataclass
class AnalysisResult:
    """Resultado bruto da análise de um arquivo (produzido nos processos de trabalho)."""

    path: str
    ok: bool = True
    error: str = ""
    size: int = 0
    mtime_ns: int = 0
    width: int = 0
    height: int = 0
    format: str = ""
    mode: str = ""
    sha256: str = ""
    phash: int = 0
    dhash: int = 0
    ahash: int = 0
    whash: int = 0
    color_sig: int = 0
    crop_phash: int = 0
    crop_dhash: int = 0
    gray_blob: bytes = b""
    desc_blob: bytes = b""
    crop_desc_blob: bytes = b""
    quality: QualityReport = field(default_factory=QualityReport)
    exif: ExifData = field(default_factory=ExifData)
    bad_flags: list[str] = field(default_factory=list)
    bad_details: dict = field(default_factory=dict)
    texture: float = 0.0
    thumb_bytes: bytes | None = None
    elapsed_ms: float = 0.0


@dataclass
class Member:
    """Uma foto dentro de um grupo de duplicatas."""

    signature: PhotoSignature
    similarity: float = 100.0
    is_reference: bool = False
    recommendation: str = "keep"        # "keep" | "remove"
    reason: str = ""
    user_choice: str | None = None      # None = segue a recomendação
    quality_report: QualityReport = field(default_factory=QualityReport)
    detail: dict = field(default_factory=dict)

    @property
    def effective_choice(self) -> str:
        return self.user_choice or self.recommendation


@dataclass
class Group:
    """Grupo de fotos consideradas a mesma imagem (ou muito próximas)."""

    group_id: int
    category: Category
    members: list[Member] = field(default_factory=list)
    status: str = "pending"             # "pending" | "reviewed" | "ignored" | "resolved"

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def reference(self) -> Member | None:
        for m in self.members:
            if m.is_reference:
                return m
        return self.members[0] if self.members else None

    @property
    def total_bytes(self) -> int:
        return sum(m.signature.size for m in self.members)

    @property
    def wasted_bytes(self) -> int:
        """Espaço que seria liberado se as recomendações/escolhas atuais fossem aplicadas."""
        if len(self.members) < 2:
            return 0
        return sum(m.signature.size for m in self.members if m.effective_choice == "remove")

    @property
    def redundant_bytes(self) -> int:
        """Espaço ocupado por todas as cópias além da melhor foto do grupo."""
        if len(self.members) < 2:
            return 0
        best = self.reference
        return sum(m.signature.size for m in self.members if m is not best)

    @property
    def selected_for_removal(self) -> list[Member]:
        return [m for m in self.members if m.effective_choice == "remove"]

    @property
    def min_similarity(self) -> float:
        sims = [m.similarity for m in self.members if not m.is_reference]
        return min(sims) if sims else 100.0


@dataclass
class ScanStats:
    """Números mostrados durante e depois da análise."""

    files_found: int = 0
    valid_photos: int = 0
    analyzed: int = 0
    skipped_cached: int = 0
    errors: int = 0
    bytes_read: int = 0
    exact_duplicates: int = 0
    visual_duplicates: int = 0
    similar_groups: int = 0
    groups_total: int = 0
    reclaimable_bytes: int = 0
    bad_photos: int = 0
    elapsed_s: float = 0.0
    rate: float = 0.0
    eta_s: float = 0.0
    phase: str = ""

    @property
    def percent(self) -> float:
        total = max(1, self.valid_photos or self.files_found)
        return min(100.0, 100.0 * (self.analyzed + self.skipped_cached) / total)

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["percent"] = round(self.percent, 1)
        return data
