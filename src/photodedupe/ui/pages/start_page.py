"""Tela inicial: escolha das pastas e início da análise."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import Settings
from ..theme import palette
from ..widgets.common import Card, number, title_block


class StartPage(QWidget):
    """Primeira tela: o usuário escolhe as pastas e manda analisar."""

    analysisRequested = Signal(list)
    resultsRequested = Signal()

    def __init__(self, settings: Settings, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.settings = settings
        self._colors = palette(theme)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        layout.addWidget(
            title_block(
                "Organizar fotos duplicadas",
                "Escolha as pastas com suas fotos. A análise é feita inteiramente neste computador "
                "e nenhum arquivo é alterado.",
            )
        )

        layout.addWidget(self._build_folders_card(), 1)
        layout.addWidget(self._build_options_card())
        layout.addWidget(self._build_privacy_card())
        layout.addLayout(self._build_footer())
        self._reload_folders()

    # ------------------------------------------------------------- montagem
    def _build_folders_card(self) -> Card:
        card = Card(self)
        header = QHBoxLayout()
        title = QLabel("Pastas a analisar")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)

        pick_one = QPushButton("Selecionar pasta")
        pick_one.setObjectName("Primary")
        pick_one.clicked.connect(lambda: self._pick_folders(replace=True))
        header.addWidget(pick_one)

        pick_many = QPushButton("Selecionar múltiplas pastas")
        pick_many.clicked.connect(lambda: self._pick_folders(replace=True, multiple=True))
        header.addWidget(pick_many)

        add = QPushButton("Adicionar pasta")
        add.clicked.connect(lambda: self._pick_folders(replace=False))
        header.addWidget(add)

        remove = QPushButton("Remover selecionada")
        remove.setObjectName("Ghost")
        remove.clicked.connect(self._remove_selected)
        header.addWidget(remove)
        card.layout().addLayout(header)

        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.folder_list.setMinimumHeight(150)
        self.folder_list.setMaximumHeight(260)
        card.layout().addWidget(self.folder_list)

        self.folder_hint = QLabel("Nenhuma pasta escolhida ainda.")
        self.folder_hint.setObjectName("StatLabel")
        card.layout().addWidget(self.folder_hint)
        return card

    def _build_options_card(self) -> Card:
        card = Card(self)
        title = QLabel("Opções da varredura")
        title.setObjectName("SectionTitle")
        card.layout().addWidget(title)

        row1 = QHBoxLayout()
        self.recursive_check = QCheckBox("Incluir subpastas")
        self.recursive_check.setChecked(self.settings.recursive)
        row1.addWidget(self.recursive_check)

        self.raw_check = QCheckBox("Incluir arquivos RAW")
        self.raw_check.setChecked(self.settings.include_raw)
        row1.addWidget(self.raw_check)

        self.hidden_check = QCheckBox("Ignorar pastas e arquivos ocultos")
        self.hidden_check.setChecked(self.settings.skip_hidden)
        row1.addWidget(self.hidden_check)

        self.bad_check = QCheckBox("Analisar também a qualidade das fotos")
        self.bad_check.setChecked(self.settings.analyze_bad_photos)
        self.bad_check.setToolTip("Identifica fotos desfocadas, escuras, prints e imagens corrompidas")
        row1.addWidget(self.bad_check)
        row1.addStretch(1)
        card.layout().addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Ignorar arquivos menores que"))
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 100_000)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setValue(self.settings.min_file_size_kb)
        row2.addWidget(self.min_size_spin)
        row2.addStretch(1)
        card.layout().addLayout(row2)
        return card

    def _build_privacy_card(self) -> Card:
        card = Card(self, margins=(16, 12, 16, 12), spacing=4)
        title = QLabel("🔒  Privacidade")
        title.setStyleSheet(f"color: {self._colors['success']}; font-weight: 700;")
        card.layout().addWidget(title)
        text = QLabel(
            "Suas fotos são analisadas localmente neste computador. Nenhuma imagem é enviada para a internet, "
            "e o aplicativo funciona totalmente offline. Durante a análise os arquivos são apenas lidos: "
            "nomes, datas, EXIF e pastas permanecem intactos."
        )
        text.setObjectName("StatLabel")
        text.setWordWrap(True)
        card.layout().addWidget(text)
        return card

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.previous_label = QLabel("")
        self.previous_label.setObjectName("StatLabel")
        row.addWidget(self.previous_label)
        row.addStretch(1)

        self.results_button = QPushButton("Ver resultados anteriores")
        self.results_button.setObjectName("Ghost")
        self.results_button.clicked.connect(self.resultsRequested.emit)
        self.results_button.setVisible(False)
        row.addWidget(self.results_button)

        self.start_button = QPushButton("▶  Iniciar análise")
        self.start_button.setObjectName("Primary")
        self.start_button.setMinimumHeight(42)
        self.start_button.setMinimumWidth(190)
        self.start_button.clicked.connect(self._start)
        row.addWidget(self.start_button)
        return row

    # --------------------------------------------------------------- ações
    def _pick_folders(self, replace: bool, multiple: bool = False) -> None:
        if multiple:
            dialog = QFileDialog(self, "Selecione uma ou mais pastas")
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
            view = dialog.findChild(QWidget, "listView")
            if view is not None:
                view.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            tree = dialog.findChild(QWidget, "treeView")
            if tree is not None:
                tree.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            if dialog.exec():
                chosen = dialog.selectedFiles()
            else:
                chosen = []
        else:
            folder = QFileDialog.getExistingDirectory(self, "Selecione a pasta com as fotos")
            chosen = [folder] if folder else []

        if not chosen:
            return
        current = [] if replace else self.folders()
        for folder in chosen:
            resolved = str(Path(folder).resolve())
            if resolved not in current:
                current.append(resolved)
        self._set_folders(current)

    def _remove_selected(self) -> None:
        for item in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(item))
        self._update_hint()

    def _set_folders(self, folders: list[str]) -> None:
        self.folder_list.clear()
        for folder in folders:
            item = QListWidgetItem(f"📁  {folder}")
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.folder_list.addItem(item)
        self._update_hint()

    def _reload_folders(self) -> None:
        self._set_folders(list(self.settings.folders))

    def _update_hint(self) -> None:
        count = self.folder_list.count()
        self.folder_hint.setText(
            "Nenhuma pasta escolhida ainda." if count == 0 else f"{number(count)} pasta(s) selecionada(s)."
        )
        self.start_button.setEnabled(count > 0)

    def folders(self) -> list[str]:
        return [
            self.folder_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.folder_list.count())
        ]

    def apply_options_to_settings(self) -> None:
        self.settings.folders = self.folders()
        self.settings.recursive = self.recursive_check.isChecked()
        self.settings.include_raw = self.raw_check.isChecked()
        self.settings.skip_hidden = self.hidden_check.isChecked()
        self.settings.analyze_bad_photos = self.bad_check.isChecked()
        self.settings.min_file_size_kb = self.min_size_spin.value()

    def _start(self) -> None:
        folders = self.folders()
        if not folders:
            return
        self.apply_options_to_settings()
        self.settings.save()
        self.analysisRequested.emit(folders)

    def set_previous_results(self, photos: int, groups: int) -> None:
        if photos:
            self.previous_label.setText(
                f"Análise anterior: {number(photos)} fotos no banco local, {number(groups)} grupos encontrados."
            )
            self.results_button.setVisible(groups > 0)
        else:
            self.previous_label.setText("")
            self.results_button.setVisible(False)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running and self.folder_list.count() > 0)
        self.start_button.setText("Analisando..." if running else "▶  Iniciar análise")
