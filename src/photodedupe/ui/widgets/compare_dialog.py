"""Comparação lado a lado de duas ou mais fotos, com zoom e mapa de diferenças."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core import imaging
from ...core.exif import ExifData
from ...core.models import QualityReport
from ...core.quality import quality_label
from ..theme import palette
from .common import ElidedLabel, QualityBar, Separator, decimal, human_size

log = logging.getLogger(__name__)

MAX_VIEW_SIDE = 1800


class ImagePane(QWidget):
    """Uma foto com zoom, rolagem sincronizada e ficha técnica."""

    scrolled = Signal(int, int)

    def __init__(self, data: dict, theme: str, show_gps: bool, parent=None):
        super().__init__(parent)
        self.data = data
        self._colors = palette(theme)
        self._pixmap = QPixmap()
        self._diff_pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._fit = True
        self._showing_diff = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet(f"background-color: {self._colors['bg']}; border-radius: 8px;")
        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setText("carregando...")
        self.scroll.setWidget(self.canvas)
        self.scroll.setMinimumHeight(300)
        layout.addWidget(self.scroll, 1)

        self.scroll.horizontalScrollBar().valueChanged.connect(self._emit_scroll)
        self.scroll.verticalScrollBar().valueChanged.connect(self._emit_scroll)

        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setFixedHeight(280)
        info_scroll.setWidget(self._build_info(show_gps))
        layout.addWidget(info_scroll)
        self.load_image()

    # -------------------------------------------------------------- imagem
    def load_image(self) -> None:
        path = self.data.get("path", "")
        try:
            image = imaging.open_for_view(path, MAX_VIEW_SIDE)
            self.pil_image = image
            qimage = _pil_to_qimage(image)
            self._pixmap = QPixmap.fromImage(qimage)
            self.fit_to_window()
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha ao abrir %s", path)
            self.pil_image = None
            self.canvas.setText(f"Não foi possível abrir a imagem:\n{exc}")

    def set_zoom(self, zoom: float) -> None:
        self._fit = False
        self._zoom = max(0.05, min(8.0, zoom))
        self._render()

    def fit_to_window(self) -> None:
        self._fit = True
        self._render()

    def zoom_percent(self) -> int:
        return int(round(self._zoom * 100))

    def _render(self) -> None:
        source = self._diff_pixmap if (self._showing_diff and self._diff_pixmap) else self._pixmap
        if source.isNull():
            return
        if self._fit:
            available = self.scroll.viewport().size() - QSize(4, 4)
            scaled = source.scaled(
                available, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self._zoom = scaled.width() / max(1, source.width())
        else:
            scaled = source.scaled(
                source.size() * self._zoom,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.canvas.setPixmap(scaled)
        self.canvas.resize(scaled.size())

    def resizeEvent(self, event):  # noqa: D102, N802
        super().resizeEvent(event)
        if self._fit:
            self._render()

    def show_difference(self, pixmap: QPixmap | None, active: bool) -> None:
        self._diff_pixmap = pixmap
        self._showing_diff = active and pixmap is not None
        self._render()

    def sync_scroll(self, x: int, y: int) -> None:
        self.scroll.horizontalScrollBar().blockSignals(True)
        self.scroll.verticalScrollBar().blockSignals(True)
        self.scroll.horizontalScrollBar().setValue(x)
        self.scroll.verticalScrollBar().setValue(y)
        self.scroll.horizontalScrollBar().blockSignals(False)
        self.scroll.verticalScrollBar().blockSignals(False)

    def _emit_scroll(self) -> None:
        self.scrolled.emit(self.scroll.horizontalScrollBar().value(), self.scroll.verticalScrollBar().value())

    # ---------------------------------------------------------- ficha técnica
    def _build_info(self, show_gps: bool) -> QWidget:
        holder = QFrame()
        holder.setObjectName("Card")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        name = ElidedLabel(Path(self.data.get("path", "")).name)
        name.setStyleSheet("font-weight: 700; font-size: 14px;")
        layout.addWidget(name)

        folder = ElidedLabel(str(Path(self.data.get("path", "")).parent))
        folder.setObjectName("StatLabel")
        layout.addWidget(folder)

        report: QualityReport = self.data.get("quality_report") or QualityReport()
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(3)
        rows = [
            ("Resolução", f"{self.data.get('width', 0)} × {self.data.get('height', 0)}"),
            ("Megapixels", decimal((self.data.get("width", 0) * self.data.get("height", 0)) / 1_000_000, 1)),
            ("Tamanho", human_size(self.data.get("size", 0))),
            ("Formato", self.data.get("format", "")),
            ("Nitidez", report.sharpness_label or "-"),
            ("Compressão", report.compression_label or "-"),
            ("Qualidade", f"{report.score:.0f}/100 ({quality_label(report.score)})"),
        ]
        if self.data.get("similarity") is not None:
            rows.append(("Semelhança", f"{decimal(float(self.data['similarity']), 1)}%"))
        for r, (label, value) in enumerate(rows):
            key = QLabel(label)
            key.setObjectName("StatLabel")
            grid.addWidget(key, r, 0)
            grid.addWidget(QLabel(str(value)), r, 1)
        layout.addLayout(grid)
        layout.addWidget(QualityBar(report.score))

        exif_raw = self.data.get("exif") or {}
        exif = _exif_from_dict(exif_raw)
        layout.addWidget(Separator())
        exif_title = QLabel("Metadados EXIF")
        exif_title.setObjectName("StatLabel")
        layout.addWidget(exif_title)
        lines = exif.summary_lines(show_gps=show_gps)
        if lines:
            exif_grid = QGridLayout()
            exif_grid.setHorizontalSpacing(14)
            exif_grid.setVerticalSpacing(2)
            for r, (label, value) in enumerate(lines):
                key = QLabel(label)
                key.setObjectName("StatLabel")
                exif_grid.addWidget(key, r, 0)
                exif_grid.addWidget(QLabel(str(value)), r, 1)
            layout.addLayout(exif_grid)
        else:
            empty = QLabel("Esta foto não tem metadados EXIF (provavelmente foi reexportada).")
            empty.setObjectName("StatLabel")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        return holder


class CompareDialog(QDialog):
    """Janela de comparação (2 a 4 fotos por vez)."""

    def __init__(self, photos: list[dict], theme: str = "dark", show_gps: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparação lado a lado")
        self.setMinimumSize(1100, 720)
        self._panes: list[ImagePane] = []
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._build_toolbar())

        panes_row = QHBoxLayout()
        panes_row.setSpacing(12)
        for data in photos[:4]:
            pane = ImagePane(data, theme, show_gps)
            pane.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            pane.scrolled.connect(self._sync_scroll)
            panes_row.addWidget(pane, 1)
            self._panes.append(pane)
        layout.addLayout(panes_row, 1)

        self.diff_info = QLabel("")
        self.diff_info.setObjectName("StatLabel")
        layout.addWidget(self.diff_info)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close = QPushButton("Fechar")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def _build_toolbar(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        fit = QPushButton("Ajustar à janela")
        fit.clicked.connect(self._fit_all)
        row.addWidget(fit)

        actual = QPushButton("100%")
        actual.clicked.connect(lambda: self._set_zoom(100))
        row.addWidget(actual)

        row.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(10, 800)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(220)
        self.zoom_slider.valueChanged.connect(self._set_zoom)
        row.addWidget(self.zoom_slider)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("StatLabel")
        row.addWidget(self.zoom_label)

        row.addStretch(1)
        self.diff_check = QCheckBox("Mostrar diferenças")
        self.diff_check.setToolTip(
            "Destaca em cores os pontos onde as imagens diferem, usando a primeira foto como referência"
        )
        self.diff_check.toggled.connect(self._toggle_difference)
        row.addWidget(self.diff_check)
        return holder

    # ------------------------------------------------------------- ações
    def _fit_all(self) -> None:
        for pane in self._panes:
            pane.fit_to_window()
        if self._panes:
            self.zoom_label.setText(f"{self._panes[0].zoom_percent()}%")

    def _set_zoom(self, percent: int) -> None:
        self.zoom_label.setText(f"{percent}%")
        for pane in self._panes:
            pane.set_zoom(percent / 100.0)

    def _sync_scroll(self, x: int, y: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        sender = self.sender()
        for pane in self._panes:
            if pane is not sender:
                pane.sync_scroll(x, y)
        self._syncing = False

    def _toggle_difference(self, active: bool) -> None:
        if not active:
            for pane in self._panes:
                pane.show_difference(None, False)
            self.diff_info.setText("")
            return
        if len(self._panes) < 2 or self._panes[0].pil_image is None:
            self.diff_info.setText("É preciso ter duas imagens abertas para comparar diferenças.")
            return
        reference = self._panes[0].pil_image
        messages = []
        for index, pane in enumerate(self._panes[1:], start=1):
            if pane.pil_image is None:
                continue
            pixmap, changed = _difference_pixmap(reference, pane.pil_image)
            pane.show_difference(pixmap, True)
            messages.append(f"Foto {index + 1}: {decimal(changed * 100, 1)}% dos pixels diferem da primeira")
        self.diff_info.setText("   |   ".join(messages))


# ------------------------------------------------------------------ utilidades
def _pil_to_qimage(image: Image.Image) -> QImage:
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimage = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return qimage.copy()


def _difference_pixmap(a: Image.Image, b: Image.Image) -> tuple[QPixmap, float]:
    """Mapa de calor das diferenças entre duas imagens."""
    size = (min(a.width, b.width), min(a.height, b.height))
    size = (max(64, min(size[0], 1400)), max(64, min(size[1], 1400)))
    arr_a = np.asarray(a.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.int16)
    arr_b = np.asarray(b.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.int16)
    diff = np.abs(arr_a - arr_b).astype(np.float32)
    changed = float((diff > 12).mean())
    norm = np.clip(diff / max(1.0, float(diff.max())), 0, 1)
    heat = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    base = np.asarray(b.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.float32) * 0.35
    heat[:, :, 0] = np.clip(base + norm * 255, 0, 255).astype(np.uint8)
    heat[:, :, 1] = np.clip(base + norm * 120, 0, 255).astype(np.uint8)
    heat[:, :, 2] = np.clip(base, 0, 255).astype(np.uint8)
    image = QImage(heat.tobytes(), size[0], size[1], size[0] * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy()), changed


def _exif_from_dict(data: dict) -> ExifData:
    exif = ExifData()
    for key, value in (data or {}).items():
        if hasattr(exif, key):
            try:
                setattr(exif, key, value)
            except Exception:  # noqa: BLE001
                continue
    return exif
