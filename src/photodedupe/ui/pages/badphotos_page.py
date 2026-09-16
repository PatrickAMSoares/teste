"""Tela de fotos com possível problema (desfocadas, escuras, prints...)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...config import Settings
from ...core.models import BadFlag
from ...db.repository import Repository
from ..theme import palette, quality_color
from ..widgets.common import Card, ElidedLabel, human_size, number, title_block
from ..widgets.flow_layout import FlowLayout

FLAG_OPTIONS = [("Todos os problemas", "")] + [(flag.label, flag.value) for flag in BadFlag]

ORDER_OPTIONS = [
    ("Menor qualidade primeiro", "quality_asc"),
    ("Maior qualidade primeiro", "quality_desc"),
    ("Maior arquivo", "size_desc"),
    ("Menor resolução", "resolution_asc"),
    ("Maior resolução", "resolution_desc"),
    ("Data da foto (mais recente)", "date_desc"),
    ("Pasta", "folder"),
]


class BadPhotoCard(Card):
    """Miniatura + problemas detectados + caixa de seleção."""

    selectionChanged = Signal()

    def __init__(self, data: dict, theme: str, parent=None):
        super().__init__(parent, margins=(10, 10, 10, 10), spacing=6)
        self.data = data
        self.file_id = int(data["file_id"])
        colors = palette(theme)
        self.setFixedWidth(206)

        self.thumb = QLabel("carregando...")
        self.thumb.setFixedSize(QSize(186, 150))
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(f"background-color: {colors['bg']}; border-radius: 6px; color: {colors['muted']};")
        self._layout.addWidget(self.thumb)

        name = ElidedLabel(Path(data["path"]).name)
        name.setStyleSheet("font-weight: 600;")
        self._layout.addWidget(name)

        info = QLabel(f"{data['width']}×{data['height']} · {human_size(data['size'])}")
        info.setObjectName("StatLabel")
        self._layout.addWidget(info)

        flags = [f for f in (data.get("bad_flags") or "").split(",") if f]
        labels = []
        for flag in flags:
            try:
                labels.append(BadFlag(flag).label)
            except ValueError:
                labels.append(flag)
        problems = QLabel(", ".join(labels) or "-")
        problems.setStyleSheet(f"color: {colors['warning']}; font-size: 11px;")
        problems.setWordWrap(True)
        self._layout.addWidget(problems)

        quality = float(data.get("quality") or 0)
        score = QLabel(f"Qualidade: {quality:.0f}/100")
        score.setStyleSheet(f"color: {quality_color(quality, theme)}; font-size: 11px; font-weight: 600;")
        self._layout.addWidget(score)

        self.check = QCheckBox("Selecionar")
        self.check.toggled.connect(lambda _: self.selectionChanged.emit())
        self._layout.addWidget(self.check)

    def set_pixmap(self, pixmap) -> None:
        if pixmap is None or pixmap.isNull():
            self.thumb.setText("sem pré-visualização")
            return
        self.thumb.setPixmap(
            pixmap.scaled(self.thumb.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )


class BadPhotosPage(QWidget):
    """Lista as fotos marcadas pela análise de qualidade."""

    statusMessage = Signal(str)
    manualRemovalRequested = Signal(list)
    detailsRequested = Signal(int)

    def __init__(self, repo: Repository, settings: Settings, thumbnails, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self.thumbnails = thumbnails
        self._theme = theme
        self._cards: dict[int, BadPhotoCard] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)
        layout.addWidget(
            title_block(
                "Fotos com possível problema",
                "Detecção independente da busca por duplicatas. Nada é removido automaticamente — "
                "estas fotos só ficam sinalizadas para a sua revisão.",
            )
        )

        bar = Card(self, margins=(14, 10, 14, 10), spacing=8)
        row = QHBoxLayout()
        row.addWidget(QLabel("Problema"))
        self.flag_combo = QComboBox()
        for label, _value in FLAG_OPTIONS:
            self.flag_combo.addItem(label)
        self.flag_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.flag_combo)

        row.addWidget(QLabel("Ordenar por"))
        self.order_combo = QComboBox()
        for label, _value in ORDER_OPTIONS:
            self.order_combo.addItem(label)
        self.order_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.order_combo)
        row.addStretch(1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("Subtitle")
        row.addWidget(self.count_label)
        bar.layout().addLayout(row)
        layout.addWidget(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.flow = FlowLayout(self.container, margin=0, spacing=12)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        self.selection_label = QLabel("Nenhuma foto selecionada.")
        footer.addWidget(self.selection_label, 1)

        select_all = QPushButton("Selecionar todas as visíveis")
        select_all.clicked.connect(lambda: self._set_all(True))
        footer.addWidget(select_all)

        clear = QPushButton("Limpar seleção")
        clear.setObjectName("Ghost")
        clear.clicked.connect(lambda: self._set_all(False))
        footer.addWidget(clear)

        self.remove_button = QPushButton("Enviar selecionadas para a quarentena")
        self.remove_button.setObjectName("Danger")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._request_removal)
        footer.addWidget(self.remove_button)
        layout.addLayout(footer)

        self.thumbnails.loaded.connect(self._on_thumbnail)

    # ---------------------------------------------------------------- dados
    def reload(self) -> None:
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        flag = FLAG_OPTIONS[self.flag_combo.currentIndex()][1]
        order = ORDER_OPTIONS[self.order_combo.currentIndex()][1]
        filters = {"only_bad": True}
        if flag:
            filters["bad_flag"] = flag
        photos = self.repo.filtered_photos(filters, order=order, limit=600)
        for data in photos:
            card = BadPhotoCard(data, self._theme)
            card.selectionChanged.connect(self._update_selection)
            self.flow.addWidget(card)
            self._cards[card.file_id] = card
            pixmap = self.thumbnails.request(card.file_id, data.get("thumb") or "", data["path"], 240)
            if pixmap is not None:
                card.set_pixmap(pixmap)
        summary = self.repo.summary()
        self.count_label.setText(
            f"{number(len(photos))} exibidas   ·   {number(summary['bad_photos'])} fotos sinalizadas no total"
        )
        self._update_selection()

    def _on_thumbnail(self, file_id: int, pixmap) -> None:
        card = self._cards.get(file_id)
        if card is not None:
            card.set_pixmap(pixmap)

    def _set_all(self, checked: bool) -> None:
        for card in self._cards.values():
            card.check.setChecked(checked)
        self._update_selection()

    def selected_ids(self) -> list[int]:
        return [fid for fid, card in self._cards.items() if card.check.isChecked()]

    def _update_selection(self) -> None:
        selected = self.selected_ids()
        total = sum(int(self._cards[fid].data["size"] or 0) for fid in selected)
        self.remove_button.setEnabled(bool(selected))
        self.selection_label.setText(
            "Nenhuma foto selecionada."
            if not selected
            else f"{number(len(selected))} foto(s) selecionadas · {human_size(total)}"
        )

    def _request_removal(self) -> None:
        selected = self.selected_ids()
        if selected:
            self.manualRemovalRequested.emit(selected)
