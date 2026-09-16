"""Tela de progresso da análise."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.fileops import format_bytes
from ...core.models import ScanStats
from ..theme import palette
from ..widgets.common import Card, StatTile, decimal, number, title_block

PHASE_LABELS = {
    "descoberta": "Procurando fotos",
    "analise": "Analisando fotos",
    "agrupamento": "Comparando e agrupando",
    "concluido": "Análise concluída",
    "cancelado": "Análise cancelada",
    "erro": "Erro na análise",
}


class ScanPage(QWidget):
    """Mostra progresso, velocidade, tempo restante e o que já foi encontrado."""

    pauseRequested = Signal()
    resumeRequested = Signal()
    cancelRequested = Signal()
    resultsRequested = Signal()

    def __init__(self, theme: str = "dark", parent=None):
        super().__init__(parent)
        self._colors = palette(theme)
        self._paused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        layout.addWidget(title_block("Análise em andamento", "Você pode pausar e retomar quando quiser."))
        layout.addWidget(self._build_progress_card())
        layout.addWidget(self._build_stats_card())
        layout.addWidget(self._build_log_card(), 1)
        layout.addLayout(self._build_buttons())

    # ------------------------------------------------------------- montagem
    def _build_progress_card(self) -> Card:
        card = Card(self)
        self.phase_label = QLabel("Preparando...")
        self.phase_label.setObjectName("SectionTitle")
        card.layout().addWidget(self.phase_label)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("StatLabel")
        self.detail_label.setWordWrap(True)
        card.layout().addWidget(self.detail_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        card.layout().addWidget(self.progress)

        self.headline = QLabel("Analisando 0 de 0 fotos")
        self.headline.setStyleSheet("font-size: 15px; font-weight: 600;")
        card.layout().addWidget(self.headline)
        return card

    def _build_stats_card(self) -> Card:
        card = Card(self)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles = {
            "found": StatTile("Arquivos encontrados"),
            "valid": StatTile("Fotos válidas"),
            "analyzed": StatTile("Já analisadas"),
            "percent": StatTile("Progresso"),
            "rate": StatTile("Velocidade"),
            "eta": StatTile("Tempo restante"),
            "duplicates": StatTile("Duplicatas encontradas", accent=self._colors["danger"]),
            "groups": StatTile("Grupos semelhantes", accent=self._colors["accent"]),
            "errors": StatTile("Arquivos com erro"),
            "bad": StatTile("Fotos com problema"),
        }
        for index, tile in enumerate(self.tiles.values()):
            grid.addWidget(tile, index // 5, index % 5)
        card.layout().addLayout(grid)
        return card

    def _build_log_card(self) -> Card:
        card = Card(self)
        title = QLabel("Registro da análise")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("Mono")
        self.log.setMaximumBlockCount(600)
        card.layout().addWidget(self.log)
        return card

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        self.pause_button = QPushButton("⏸  Pausar análise")
        self.pause_button.clicked.connect(self._toggle_pause)
        row.addWidget(self.pause_button)

        self.cancel_button = QPushButton("Cancelar análise")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        row.addWidget(self.cancel_button)

        self.results_button = QPushButton("Ver resultados  →")
        self.results_button.setObjectName("Primary")
        self.results_button.setVisible(False)
        self.results_button.clicked.connect(self.resultsRequested.emit)
        row.addWidget(self.results_button)
        return row

    # --------------------------------------------------------------- ações
    def _toggle_pause(self) -> None:
        if self._paused:
            self._paused = False
            self.pause_button.setText("⏸  Pausar análise")
            self.resumeRequested.emit()
        else:
            self._paused = True
            self.pause_button.setText("▶  Retomar análise")
            self.pauseRequested.emit()

    def reset(self) -> None:
        self._paused = False
        self.pause_button.setText("⏸  Pausar análise")
        self.pause_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.results_button.setVisible(False)
        self.progress.setValue(0)
        self.log.clear()
        for tile in self.tiles.values():
            tile.set_value("0")
        self.phase_label.setText("Preparando...")
        self.headline.setText("Analisando 0 de 0 fotos")

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(message)

    def set_phase(self, phase: str, message: str) -> None:
        self.phase_label.setText(PHASE_LABELS.get(phase, phase.capitalize()))
        self.detail_label.setText(message)
        if phase in ("concluido", "cancelado", "erro"):
            self.pause_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.results_button.setVisible(phase == "concluido")

    def update_stats(self, stats: ScanStats) -> None:
        total = stats.valid_photos or stats.files_found
        done = stats.analyzed + stats.skipped_cached
        self.progress.setValue(int(stats.percent * 10))
        self.headline.setText(
            f"Analisando {number(done)} de {number(total)} fotos   ·   Progresso: {decimal(stats.percent, 1)}%"
        )
        self.tiles["found"].set_value(number(stats.files_found))
        self.tiles["valid"].set_value(number(stats.valid_photos))
        self.tiles["analyzed"].set_value(number(done))
        self.tiles["percent"].set_value(f"{decimal(stats.percent, 1)}%")
        self.tiles["rate"].set_value(f"{decimal(stats.rate, 1)}/s")
        self.tiles["eta"].set_value(_format_eta(stats.eta_s))
        self.tiles["duplicates"].set_value(number(stats.exact_duplicates + stats.visual_duplicates))
        self.tiles["groups"].set_value(number(stats.groups_total))
        self.tiles["errors"].set_value(number(stats.errors))
        self.tiles["bad"].set_value(number(stats.bad_photos))

    def show_final(self, stats: ScanStats) -> None:
        self.update_stats(stats)
        self.detail_label.setText(
            f"Tempo total: {_format_eta(stats.elapsed_s)}  ·  "
            f"Espaço potencialmente liberável: {format_bytes(stats.reclaimable_bytes)}"
        )


def _format_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}min"
