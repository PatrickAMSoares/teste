"""Janela principal: navegação entre as telas e coordenação das operações."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..config import Settings
from ..core.fileops import HAS_SEND2TRASH, MODE_QUARANTINE, FileManager, format_bytes
from ..db.database import Database
from ..db.repository import Repository
from ..paths import reports_dir
from .pages.badphotos_page import BadPhotosPage
from .pages.groups_page import GroupsPage
from .pages.report_page import ReportPage
from .pages.review_page import ReviewPage
from .pages.scan_page import ScanPage
from .pages.settings_page import SettingsPage
from .pages.start_page import StartPage
from .theme import stylesheet
from .widgets.compare_dialog import CompareDialog
from .widgets.dialogs import ConfirmDeletionDialog, PhotoDetailsDialog
from .workers import AnalysisWorker, FileOperationWorker, RegroupWorker, ThumbnailLoader

log = logging.getLogger(__name__)

PAGE_START, PAGE_SCAN, PAGE_GROUPS, PAGE_REVIEW, PAGE_BAD, PAGE_REPORT, PAGE_SETTINGS = range(7)

NAV_ITEMS = [
    ("🏠  Início", PAGE_START),
    ("🔍  Análise", PAGE_SCAN),
    ("🗂  Duplicatas", PAGE_GROUPS),
    ("✅  Revisar", PAGE_REVIEW),
    ("⚠  Fotos com problema", PAGE_BAD),
    ("📊  Relatório", PAGE_REPORT),
    ("⚙  Configurações", PAGE_SETTINGS),
]


class MainWindow(QMainWindow):
    """Monta as telas, conecta os sinais e cuida do ciclo de vida das operações."""

    def __init__(self, db: Database, settings: Settings, parent=None):
        super().__init__(parent)
        self.db = db
        self.repo = Repository(db)
        self.settings = settings
        self.worker: AnalysisWorker | None = None
        self.file_worker: FileOperationWorker | None = None
        self.regroup_worker: RegroupWorker | None = None
        self.thumbnails = ThumbnailLoader()

        self.setWindowTitle(f"{APP_DISPLAY_NAME}  {__version__}")
        self.resize(1440, 920)
        self.setMinimumSize(1120, 720)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._build_pages()
        self._build_shortcuts()
        self.statusBar().showMessage("Pronto.")
        self.apply_theme(self.settings.theme)
        self._refresh_start_page()
        QTimer.singleShot(200, self._warn_missing_backends)

    # ------------------------------------------------------------ montagem
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(0)

        title = QLabel("PhotoDedupe")
        title.setObjectName("SidebarTitle")
        layout.addWidget(title)
        subtitle = QLabel("Organizador de fotos duplicadas\n100% local e offline")
        subtitle.setObjectName("SidebarSubtitle")
        layout.addWidget(subtitle)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for label, index in NAV_ITEMS:
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self.go_to(i))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)
        self.sidebar_status = QLabel("")
        self.sidebar_status.setObjectName("SidebarSubtitle")
        self.sidebar_status.setWordWrap(True)
        layout.addWidget(self.sidebar_status)
        return sidebar

    def _build_pages(self) -> None:
        theme = self.settings.theme
        self.start_page = StartPage(self.settings, theme)
        self.scan_page = ScanPage(theme)
        self.groups_page = GroupsPage(self.repo, self.settings, self.thumbnails, theme)
        self.review_page = ReviewPage(self.repo, self.settings, self.thumbnails, theme)
        self.bad_page = BadPhotosPage(self.repo, self.settings, self.thumbnails, theme)
        self.report_page = ReportPage(self.repo, self.settings, theme)
        self.settings_page = SettingsPage(self.repo, self.db, self.settings, theme)

        for page in (
            self.start_page, self.scan_page, self.groups_page, self.review_page,
            self.bad_page, self.report_page, self.settings_page,
        ):
            self.stack.addWidget(page)

        self.start_page.analysisRequested.connect(self.start_analysis)
        self.start_page.resultsRequested.connect(lambda: self.go_to(PAGE_GROUPS))

        self.scan_page.pauseRequested.connect(self._pause)
        self.scan_page.resumeRequested.connect(self._resume)
        self.scan_page.cancelRequested.connect(self._cancel)
        self.scan_page.resultsRequested.connect(lambda: self.go_to(PAGE_GROUPS))

        self.groups_page.removalRequested.connect(self.confirm_removal)
        self.groups_page.statusMessage.connect(self.show_status)
        self.groups_page.compareRequested.connect(self.open_compare)
        self.groups_page.detailsRequested.connect(self.open_details)

        self.review_page.removalRequested.connect(self.confirm_removal)
        self.review_page.statusMessage.connect(self.show_status)
        self.review_page.compareRequested.connect(self.open_compare)
        self.review_page.detailsRequested.connect(self.open_details)

        self.bad_page.statusMessage.connect(self.show_status)
        self.bad_page.manualRemovalRequested.connect(self.confirm_manual_removal)
        self.bad_page.detailsRequested.connect(self.open_details)

        self.report_page.statusMessage.connect(self.show_status)
        self.report_page.undoRequested.connect(self.undo_batch)

        self.settings_page.statusMessage.connect(self.show_status)
        self.settings_page.regroupRequested.connect(self.regroup)
        self.settings_page.themeChanged.connect(self.apply_theme)

    def _build_shortcuts(self) -> None:
        actions = [
            ("Iniciar análise", "Ctrl+R", lambda: self.start_page._start()),
            ("Ir para duplicatas", "Ctrl+2", lambda: self.go_to(PAGE_GROUPS)),
            ("Ir para revisão", "Ctrl+3", lambda: self.go_to(PAGE_REVIEW)),
            ("Relatório", "Ctrl+4", lambda: self.go_to(PAGE_REPORT)),
            ("Configurações", "Ctrl+,", lambda: self.go_to(PAGE_SETTINGS)),
            ("Sair", "Ctrl+Q", self.close),
        ]
        for name, shortcut, handler in actions:
            action = QAction(name, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            self.addAction(action)

    # ------------------------------------------------------------ navegação
    def go_to(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        button = self.nav_group.button(index)
        if button is not None:
            button.setChecked(True)
        if index == PAGE_GROUPS:
            self.groups_page.refresh_choices()
            self.groups_page.reload()
        elif index == PAGE_REVIEW:
            self.review_page.reload()
        elif index == PAGE_BAD:
            self.bad_page.reload()
        elif index == PAGE_REPORT:
            self.report_page.reload()
        elif index == PAGE_START:
            self._refresh_start_page()

    def show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    def apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet(theme))
        self.settings.theme = theme

    def _refresh_start_page(self) -> None:
        summary = self.repo.summary()
        self.start_page.set_previous_results(summary["photos"], summary["groups"])
        self.sidebar_status.setText(
            f"{summary['photos']:n} fotos no banco local\n{summary['groups']:n} grupos encontrados"
            if summary["photos"]
            else "Nenhuma análise feita ainda"
        )

    def _warn_missing_backends(self) -> None:
        from ..core.imaging import HAS_HEIF, HAS_RAWPY

        missing = []
        if not HAS_HEIF:
            missing.append("HEIC/HEIF (fotos de iPhone)")
        if not HAS_RAWPY:
            missing.append("RAW de câmeras")
        if missing:
            self.show_status(
                "Formatos sem suporte nesta instalação: " + ", ".join(missing) + ". Veja Configurações › Desempenho."
            )

    # -------------------------------------------------------------- análise
    def start_analysis(self, folders: list[str]) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Análise em andamento", "Já existe uma análise rodando.")
            return
        for folder in folders:
            self.repo.add_folder(folder, self.settings.recursive)

        self.scan_page.reset()
        self.go_to(PAGE_SCAN)
        self.start_page.set_running(True)

        self.worker = AnalysisWorker(self.repo, self.settings, folders)
        self.worker.statsChanged.connect(self.scan_page.update_stats)
        self.worker.phaseChanged.connect(self.scan_page.set_phase)
        self.worker.logMessage.connect(self.scan_page.append_log)
        self.worker.failed.connect(self._on_analysis_error)
        self.worker.completed.connect(self._on_analysis_done)
        self.worker.start()
        self.show_status("Análise iniciada.")

    def _pause(self) -> None:
        if self.worker:
            self.worker.pause()
            self.show_status("Análise pausada. Clique em “Retomar” para continuar.")

    def _resume(self) -> None:
        if self.worker:
            self.worker.resume()
            self.show_status("Análise retomada.")

    def _cancel(self) -> None:
        if not self.worker:
            return
        answer = QMessageBox.question(
            self,
            "Cancelar análise",
            "O progresso já gravado é mantido e a análise pode ser retomada depois.\n\nCancelar agora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.worker.cancel()
            self.show_status("Cancelando...")

    def _on_analysis_error(self, message: str) -> None:
        QMessageBox.critical(self, "Erro na análise", message)

    def _on_analysis_done(self, stats) -> None:
        self.start_page.set_running(False)
        self.scan_page.show_final(stats)
        self._refresh_start_page()
        if stats.phase == "concluido":
            self.show_status(
                f"Análise concluída: {stats.groups_total:n} grupos, "
                f"{format_bytes(stats.reclaimable_bytes)} potencialmente liberáveis."
            )
            self.groups_page.refresh_choices()
        else:
            self.show_status("Análise interrompida.")

    def regroup(self) -> None:
        if self.regroup_worker is not None and self.regroup_worker.isRunning():
            return
        self.show_status("Reagrupando com os novos limites...")
        self.regroup_worker = RegroupWorker(self.repo, self.settings)
        self.regroup_worker.completed.connect(self._on_regroup_done)
        self.regroup_worker.failed.connect(lambda msg: QMessageBox.critical(self, "Erro ao reagrupar", msg))
        self.regroup_worker.start()

    def _on_regroup_done(self, count: int) -> None:
        self.show_status(f"Reagrupamento concluído: {count:n} grupos.")
        self._refresh_start_page()
        if self.stack.currentIndex() == PAGE_GROUPS:
            self.groups_page.reload()

    # ------------------------------------------------------------- remoção
    def confirm_removal(self) -> None:
        manager = self._file_manager()
        plan = manager.build_plan(self.settings.deletion_mode)
        if not plan.items:
            QMessageBox.information(
                self,
                "Nada marcado",
                "Nenhum arquivo está marcado para remoção.\n\n"
                "Use “Aceitar recomendação” nos grupos ou marque as fotos manualmente.",
            )
            return
        self._run_plan(plan, manager)

    def confirm_manual_removal(self, file_ids: list[int]) -> None:
        manager = self._file_manager()
        plan = manager.build_manual_plan(file_ids, self.settings.deletion_mode, "seleção manual")
        if not plan.items:
            return
        self._run_plan(plan, manager)

    def _run_plan(self, plan, manager: FileManager) -> None:
        dialog = ConfirmDeletionDialog(plan, self.settings.theme, allow_trash=HAS_SEND2TRASH, parent=self)
        if not dialog.exec():
            self.show_status("Operação cancelada. Nenhum arquivo foi tocado.")
            return
        if dialog.export_path:
            try:
                manager.export_plan(plan, dialog.export_path)
                self.show_status(f"Lista exportada para {dialog.export_path}")
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Falha ao exportar", str(exc))

        progress = QProgressDialog("Movendo arquivos...", "", 0, max(1, plan.count), self)
        progress.setWindowTitle("Removendo")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        self.file_worker = FileOperationWorker(manager, plan=plan)
        self.file_worker.progress.connect(lambda done, total: progress.setValue(done))
        self.file_worker.completed.connect(lambda result: self._on_files_done(result, progress, undo=False))
        self.file_worker.failed.connect(lambda msg: self._on_files_failed(msg, progress))
        self.file_worker.start()

    def undo_batch(self, batch: str) -> None:
        manager = self._file_manager()
        actions = self.repo.batch_actions(batch)
        restorable = [a for a in actions if a["action"] == "quarentena" and a["status"] == "ok"]
        if not restorable:
            QMessageBox.information(
                self,
                "Não é possível desfazer",
                "Este lote não tem arquivos na quarentena.\n\n"
                "Arquivos enviados para a Lixeira devem ser restaurados pela própria Lixeira do Windows.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Desfazer remoção",
            f"{len(restorable)} arquivo(s) voltarão para as pastas de origem.\n\nContinuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        progress = QProgressDialog("Restaurando arquivos...", "", 0, len(restorable), self)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setValue(0)

        self.file_worker = FileOperationWorker(manager, undo_batch=batch)
        self.file_worker.progress.connect(lambda done, total: progress.setValue(done))
        self.file_worker.completed.connect(lambda result: self._on_files_done(result, progress, undo=True))
        self.file_worker.failed.connect(lambda msg: self._on_files_failed(msg, progress))
        self.file_worker.start()

    def _on_files_done(self, result, progress: QProgressDialog, undo: bool) -> None:
        progress.close()
        if undo:
            text = f"{result.moved} arquivo(s) restaurados."
        else:
            text = (
                f"{result.moved} arquivo(s) movidos para {result.destination}.\n"
                f"Espaço liberado: {format_bytes(result.freed_bytes)}.\n\n"
                "Você pode desfazer esta operação na tela de Relatório."
            )
        if result.failed:
            text += f"\n\n{result.failed} arquivo(s) não puderam ser processados."
            details = "\n".join(f"• {path}: {error}" for path, error in result.errors[:12])
            box = QMessageBox(QMessageBox.Icon.Warning, "Operação concluída com avisos", text, parent=self)
            box.setDetailedText(details)
            box.exec()
        else:
            QMessageBox.information(self, "Operação concluída", text)
        self.show_status(text.splitlines()[0])
        self.groups_page.reload()
        self.report_page.reload()
        self._refresh_start_page()

    def _on_files_failed(self, message: str, progress: QProgressDialog) -> None:
        progress.close()
        QMessageBox.critical(self, "Falha na operação", message)

    def _file_manager(self) -> FileManager:
        return FileManager(
            self.repo,
            self.settings.quarantine_path(),
            self.settings.keep_folder_structure_in_quarantine,
        )

    # -------------------------------------------------------------- janelas
    def open_compare(self, file_ids: list[int]) -> None:
        photos = []
        for file_id in file_ids[:4]:
            details = self.repo.photo_details(file_id)
            if details:
                photos.append(details)
        if len(photos) < 2:
            QMessageBox.information(self, "Comparação", "É preciso ao menos duas fotos para comparar.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            dialog = CompareDialog(photos, self.settings.theme, self.settings.show_gps_in_ui, self)
        finally:
            QApplication.restoreOverrideCursor()
        dialog.exec()

    def open_details(self, file_id: int) -> None:
        details = self.repo.photo_details(file_id)
        if not details:
            return
        PhotoDetailsDialog(details, self.settings.theme, self.settings.show_gps_in_ui, self).exec()

    # ----------------------------------------------------------- fechamento
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Sair do aplicativo",
                "Há uma análise em andamento. O progresso já gravado é mantido e pode ser retomado depois.\n\nSair agora?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(8000)
        if self.file_worker is not None and self.file_worker.isRunning():
            self.file_worker.wait(15000)
        self.thumbnails.shutdown()
        try:
            self.settings.save()
        except Exception:  # noqa: BLE001
            log.exception("Falha ao salvar configurações")
        event.accept()
