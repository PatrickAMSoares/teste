"""Tela de relatório: resumo da análise, exportações e histórico de operações."""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import Settings
from ...core import reports
from ...core.fileops import FileManager, format_bytes
from ...db.repository import Repository
from ...paths import reports_dir
from ..theme import palette
from ..widgets.common import Card, StatTile, number, title_block

log = logging.getLogger(__name__)


class ReportPage(QWidget):
    """Mostra o resumo final e permite exportar em CSV, Excel, JSON e PDF."""

    statusMessage = Signal(str)
    undoRequested = Signal(str)

    def __init__(self, repo: Repository, settings: Settings, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self._colors = palette(theme)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(14)
        layout.addWidget(title_block("Relatório da análise", "Resumo do que foi encontrado e exportação dos resultados."))

        self.tiles_card = Card(self)
        self.tiles_grid = QGridLayout()
        self.tiles_grid.setSpacing(10)
        self.tiles = {
            "photos": StatTile("Fotos analisadas"),
            "exact": StatTile("Duplicatas exatas", accent=self._colors["danger"]),
            "visual": StatTile("Duplicatas visuais", accent=self._colors["warning"]),
            "very": StatTile("Grupos muito semelhantes"),
            "similar": StatTile("Grupos semelhantes"),
            "reclaim": StatTile("Espaço liberável", accent=self._colors["success"]),
            "bad": StatTile("Fotos com problema"),
            "errors": StatTile("Arquivos com erro"),
        }
        for index, tile in enumerate(self.tiles.values()):
            self.tiles_grid.addWidget(tile, index // 4, index % 4)
        self.tiles_card.layout().addLayout(self.tiles_grid)
        layout.addWidget(self.tiles_card)

        export_card = Card(self)
        title = QLabel("Exportar relatório")
        title.setObjectName("SectionTitle")
        export_card.layout().addWidget(title)
        hint = QLabel(
            "Exporte antes de remover qualquer arquivo: o relatório registra exatamente o que o "
            "aplicativo recomendou e o que você escolheu."
        )
        hint.setObjectName("StatLabel")
        hint.setWordWrap(True)
        export_card.layout().addWidget(hint)

        row = QHBoxLayout()
        for label, fmt in (("CSV", "csv"), ("Excel (.xlsx)", "xlsx"), ("JSON", "json"), ("PDF", "pdf")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, f=fmt: self.export(f))
            row.addWidget(button)
        row.addStretch(1)
        open_folder = QPushButton("Abrir pasta de relatórios")
        open_folder.setObjectName("Ghost")
        open_folder.clicked.connect(self._open_reports_folder)
        row.addWidget(open_folder)
        export_card.layout().addLayout(row)
        layout.addWidget(export_card)

        history_card = Card(self)
        history_title = QLabel("Operações realizadas")
        history_title.setObjectName("SectionTitle")
        history_card.layout().addWidget(history_title)
        history_hint = QLabel(
            "Cada remoção fica registrada aqui. Enquanto os arquivos estiverem na quarentena, é possível desfazer."
        )
        history_hint.setObjectName("StatLabel")
        history_card.layout().addWidget(history_hint)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["Lote", "Data", "Arquivos", "Tamanho", "Ação"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setMinimumHeight(180)
        history_card.layout().addWidget(self.history_table)

        history_row = QHBoxLayout()
        self.quarantine_label = QLabel("")
        self.quarantine_label.setObjectName("StatLabel")
        history_row.addWidget(self.quarantine_label, 1)
        self.undo_button = QPushButton("Desfazer lote selecionado")
        self.undo_button.clicked.connect(self._undo_selected)
        history_row.addWidget(self.undo_button)
        history_card.layout().addLayout(history_row)
        layout.addWidget(history_card, 1)

    # ---------------------------------------------------------------- dados
    def reload(self) -> None:
        summary = self.repo.summary()
        self.tiles["photos"].set_value(number(summary["photos"]))
        self.tiles["exact"].set_value(number(summary["exact_duplicates"]))
        self.tiles["visual"].set_value(number(summary["visual_duplicates"]))
        self.tiles["very"].set_value(number(summary["very_similar_groups"]))
        self.tiles["similar"].set_value(number(summary["similar_groups"]))
        self.tiles["reclaim"].set_value(format_bytes(summary["reclaimable_bytes"]))
        self.tiles["bad"].set_value(number(summary["bad_photos"]))
        self.tiles["errors"].set_value(number(summary["errors"]))

        batches = self.repo.list_batches(50)
        self.history_table.setRowCount(len(batches))
        for row, batch in enumerate(batches):
            self.history_table.setItem(row, 0, QTableWidgetItem(batch["batch"]))
            self.history_table.setItem(row, 1, QTableWidgetItem(str(batch["ts"])))
            self.history_table.setItem(row, 2, QTableWidgetItem(number(batch["n"])))
            self.history_table.setItem(row, 3, QTableWidgetItem(format_bytes(batch["bytes"] or 0)))
            status = "desfeito" if not batch["ok_count"] else batch["action"]
            self.history_table.setItem(row, 4, QTableWidgetItem(status))
        self.history_table.resizeColumnsToContents()

        manager = FileManager(self.repo, self.settings.quarantine_path())
        count, total = manager.quarantine_size()
        self.quarantine_label.setText(
            f"Quarentena: {number(count)} arquivo(s), {format_bytes(total)} em {manager.quarantine_dir}"
        )
        self.undo_button.setEnabled(bool(batches))

    # -------------------------------------------------------------- ações
    def export(self, fmt: str) -> None:
        filters = {
            "csv": "CSV (*.csv)",
            "xlsx": "Planilha do Excel (*.xlsx)",
            "json": "JSON (*.json)",
            "pdf": "PDF (*.pdf)",
        }[fmt]
        suggested = str(reports_dir() / f"relatorio_photodedupe.{fmt}")
        path, _ = QFileDialog.getSaveFileName(self, "Exportar relatório", suggested, filters)
        if not path:
            return
        try:
            data = reports.collect(self.repo, self.settings.to_dict())
            target = reports.export(data, path, fmt)
            self.statusMessage.emit(f"Relatório exportado para {target}")
            QMessageBox.information(self, "Relatório exportado", f"Arquivo gravado em:\n{target}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha ao exportar relatório")
            QMessageBox.critical(self, "Erro ao exportar", str(exc))

    def _open_reports_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(reports_dir())))

    def _undo_selected(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Desfazer", "Selecione um lote na lista.")
            return
        item = self.history_table.item(row, 0)
        if item:
            self.undoRequested.emit(item.text())
