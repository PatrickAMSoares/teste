"""Diálogos de confirmação e de detalhes técnicos."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.fileops import MODE_QUARANTINE, MODE_TRASH, DeletionPlan, format_bytes
from ...core.models import QualityReport
from ...paths import reports_dir
from ..theme import palette
from .common import Separator, decimal


class ConfirmDeletionDialog(QDialog):
    """Última barreira antes de qualquer operação destrutiva.

    Mostra exatamente o que será feito, com quantos arquivos e quanto espaço, e
    exige um clique explícito em “Confirmar”. O botão em destaque é o de
    cancelar - o padrão seguro.
    """

    def __init__(self, plan: DeletionPlan, theme: str = "dark", allow_trash: bool = True, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.export_path: Path | None = None
        self._colors = palette(theme)
        self.setWindowTitle("Confirmar remoção")
        self.setMinimumSize(880, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Revise antes de continuar")
        title.setObjectName("Title")
        layout.addWidget(title)

        summary = QLabel(plan.summary_text())
        summary.setWordWrap(True)
        summary.setStyleSheet("font-size: 14px;")
        layout.addWidget(summary)

        highlight = QLabel(
            f"{plan.count} arquivo(s)   ·   {format_bytes(plan.total_bytes)} a liberar"
        )
        highlight.setStyleSheet(
            f"color: {self._colors['warning']}; font-size: 17px; font-weight: 700;"
        )
        layout.addWidget(highlight)
        layout.addWidget(Separator())

        destination = QLabel("Para onde os arquivos vão:")
        destination.setObjectName("SectionTitle")
        layout.addWidget(destination)

        self.quarantine_radio = QRadioButton(
            f"Pasta de quarentena (recomendado) — {plan.quarantine_dir}"
        )
        self.quarantine_radio.setToolTip(
            "Os arquivos são movidos para uma pasta do aplicativo e podem ser restaurados com um clique."
        )
        self.trash_radio = QRadioButton("Lixeira do sistema")
        self.trash_radio.setEnabled(allow_trash)
        if not allow_trash:
            self.trash_radio.setToolTip("Requer o pacote 'send2trash'.")
        (self.trash_radio if plan.mode == MODE_TRASH and allow_trash else self.quarantine_radio).setChecked(True)
        layout.addWidget(self.quarantine_radio)
        layout.addWidget(self.trash_radio)

        self.export_check = QCheckBox("Exportar a lista destes arquivos antes de continuar")
        self.export_check.setChecked(True)
        layout.addWidget(self.export_check)

        table_label = QLabel("Arquivos que serão movidos:")
        table_label.setObjectName("SectionTitle")
        layout.addWidget(table_label)
        layout.addWidget(self._build_table(), 1)

        if plan.blocked_items:
            blocked = QLabel(
                f"⚠ {len(plan.blocked_items)} arquivo(s) foram bloqueados por segurança "
                "(modificados, ausentes ou sem foto de referência) e não serão tocados."
            )
            blocked.setStyleSheet(f"color: {self._colors['warning']};")
            blocked.setWordWrap(True)
            layout.addWidget(blocked)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        confirm = buttons.addButton("Confirmar remoção", QDialogButtonBox.ButtonRole.AcceptRole)
        confirm.setObjectName("Danger")
        cancel.setObjectName("Primary")
        cancel.setDefault(True)
        cancel.setAutoDefault(True)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_table(self) -> QWidget:
        items = self.plan.items
        table = QTableWidget(len(items), 5)
        table.setHorizontalHeaderLabels(["Arquivo", "Tamanho", "Semelhança", "Foto mantida", "Situação"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for row, item in enumerate(items):
            table.setItem(row, 0, QTableWidgetItem(item.path))
            table.setItem(row, 1, QTableWidgetItem(format_bytes(item.size)))
            table.setItem(row, 2, QTableWidgetItem(f"{decimal(item.similarity, 1)}%"))
            table.setItem(row, 3, QTableWidgetItem(Path(item.keeper_path).name if item.keeper_path else "-"))
            status = QTableWidgetItem("será movido" if item.valid else f"bloqueado: {item.problem}")
            if not item.valid:
                status.setForeground(Qt.GlobalColor.red)
            table.setItem(row, 4, status)
        table.resizeColumnsToContents()
        table.setColumnWidth(0, 380)
        return table

    def _on_accept(self) -> None:
        self.plan.mode = MODE_TRASH if self.trash_radio.isChecked() else MODE_QUARANTINE
        if self.export_check.isChecked():
            suggested = str(reports_dir() / "plano_de_remocao.csv")
            path, _ = QFileDialog.getSaveFileName(
                self, "Exportar lista de arquivos", suggested, "CSV (*.csv);;JSON (*.json)"
            )
            if path:
                self.export_path = Path(path)
        self.accept()


class PhotoDetailsDialog(QDialog):
    """Ficha completa de uma foto, incluindo os “detalhes técnicos”."""

    def __init__(self, details: dict, theme: str = "dark", show_gps: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detalhes técnicos")
        self.setMinimumSize(720, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        path = details.get("path", "")
        title = QLabel(Path(path).name)
        title.setObjectName("Title")
        layout.addWidget(title)
        subtitle = QLabel(str(Path(path).parent))
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        report: QualityReport = details.get("quality_report") or QualityReport()
        lines = [f"Índice de qualidade: {report.score:.1f}/100", ""]
        lines += [f"  • {reason}" for reason in report.reasons]
        lines.append("")
        lines.append("Componentes da pontuação (0 a 100):")
        for key, value in report.components.items():
            lines.append(f"  • {key}: {value:.1f}")
        lines.append("")
        lines.append(f"Nitidez (variância do Laplaciano): {report.sharpness_raw:.1f}")
        lines.append(f"Bits por pixel: {report.bits_per_pixel:.3f}")
        if report.jpeg_quality_estimate:
            lines.append(f"Qualidade JPEG estimada: {report.jpeg_quality_estimate}%")
        lines.append(f"Originalidade estimada: {report.originality:.2f}")

        bad = details.get("bad_details") or {}
        if bad:
            lines.append("")
            lines.append("Métricas de qualidade da imagem:")
            for key, value in bad.items():
                lines.append(f"  • {key}: {value}")

        exif = details.get("exif") or {}
        if exif:
            lines.append("")
            lines.append("EXIF:")
            for key, value in exif.items():
                if key == "raw":
                    continue
                if key in ("gps_lat", "gps_lon") and not show_gps:
                    value = "(oculto - ative a exibição de GPS nas configurações)"
                if value in (None, "", [], {}):
                    continue
                lines.append(f"  • {key}: {value}")
            raw = exif.get("raw") or {}
            if raw:
                lines.append("")
                lines.append("EXIF bruto (parcial):")
                lines.append(json.dumps(raw, indent=2, ensure_ascii=False)[:4000])

        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setObjectName("Mono")
        layout.addWidget(text, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Fechar")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
