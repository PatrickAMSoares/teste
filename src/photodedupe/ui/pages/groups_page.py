"""Tela de resultados: navegação pelos grupos de duplicatas, com filtros."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import Settings
from ...core.fileops import format_bytes
from ...core.models import Category
from ...db.repository import Repository
from ..theme import palette
from ..widgets.common import Card, number, title_block
from ..widgets.group_card import GroupCard

log = logging.getLogger(__name__)

PAGE_SIZE = 20

CATEGORY_TABS = [
    ("Todas as categorias", []),
    (f"{Category.EXACT.emoji} Duplicatas exatas", [Category.EXACT.value]),
    (f"{Category.VISUAL.emoji} Duplicatas visuais", [Category.VISUAL.value]),
    (f"{Category.VERY_SIMILAR.emoji} Muito semelhantes", [Category.VERY_SIMILAR.value]),
    (f"{Category.SIMILAR.emoji} Semelhantes", [Category.SIMILAR.value]),
    ("Duplicatas (exatas + visuais)", [Category.EXACT.value, Category.VISUAL.value]),
]

ORDER_OPTIONS = [
    ("Maior espaço liberável", "wasted_desc"),
    ("Maior semelhança", "similarity_desc"),
    ("Menor semelhança", "similarity_asc"),
    ("Mais fotos no grupo", "size_desc"),
    ("Categoria", "category"),
    ("Ordem de descoberta", "id"),
]


class GroupsPage(QWidget):
    """Lista paginada de grupos, com filtros, ordenação e decisões do usuário."""

    removalRequested = Signal()
    statusMessage = Signal(str)
    compareRequested = Signal(list)
    detailsRequested = Signal(int)

    def __init__(self, repo: Repository, settings: Settings, thumbnails, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self.thumbnails = thumbnails
        self._theme = theme
        self._colors = palette(theme)
        self._offset = 0
        self._total = 0
        self._cards_by_file: dict[int, list[GroupCard]] = {}
        self._cards: list[GroupCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(
            title_block(
                "Duplicatas encontradas",
                "Cada grupo reúne versões da mesma foto. A recomendação é apenas técnica — você decide o que fazer.",
            ),
            1,
        )
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Subtitle")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.summary_label)
        layout.addLayout(header)

        layout.addWidget(self._build_filter_bar())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 8, 0)
        self.container_layout.setSpacing(14)
        self.container_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("Nenhum grupo para mostrar com os filtros atuais.")
        self.empty_label.setObjectName("Subtitle")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        self.more_button = QPushButton("Carregar mais grupos")
        self.more_button.clicked.connect(self.load_more)
        self.more_button.setVisible(False)
        layout.addWidget(self.more_button)

        layout.addWidget(self._build_footer())
        self.thumbnails.loaded.connect(self._on_thumbnail)

    # ------------------------------------------------------------- montagem
    def _build_filter_bar(self) -> Card:
        card = Card(self, margins=(14, 10, 14, 10), spacing=8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.category_combo = QComboBox()
        for label, _value in CATEGORY_TABS:
            self.category_combo.addItem(label)
        self.category_combo.currentIndexChanged.connect(self.reload)
        row1.addWidget(QLabel("Categoria"))
        row1.addWidget(self.category_combo)

        self.order_combo = QComboBox()
        for label, _value in ORDER_OPTIONS:
            self.order_combo.addItem(label)
        self.order_combo.currentIndexChanged.connect(self.reload)
        row1.addWidget(QLabel("Ordenar por"))
        row1.addWidget(self.order_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filtrar por nome ou pasta...")
        self.search_edit.returnPressed.connect(self.reload)
        row1.addWidget(self.search_edit, 1)

        search_button = QPushButton("Aplicar filtros")
        search_button.clicked.connect(self.reload)
        row1.addWidget(search_button)

        self.advanced_button = QPushButton("Mais filtros")
        self.advanced_button.setObjectName("Ghost")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        row1.addWidget(self.advanced_button)

        self.technical_check = QCheckBox("Detalhes técnicos")
        self.technical_check.setChecked(self.settings.show_technical_details)
        self.technical_check.toggled.connect(self._toggle_technical)
        row1.addWidget(self.technical_check)
        card.layout().addLayout(row1)

        self.advanced = QWidget()
        row2 = QHBoxLayout(self.advanced)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)

        row2.addWidget(QLabel("Formato"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Qualquer")
        row2.addWidget(self.format_combo)

        row2.addWidget(QLabel("Pasta"))
        self.folder_combo = QComboBox()
        self.folder_combo.addItem("Todas")
        row2.addWidget(self.folder_combo, 1)

        row2.addWidget(QLabel("Semelhança mínima"))
        self.similarity_spin = QDoubleSpinBox()
        self.similarity_spin.setRange(0.0, 100.0)
        self.similarity_spin.setDecimals(1)
        self.similarity_spin.setSuffix(" %")
        self.similarity_spin.setValue(0.0)
        row2.addWidget(self.similarity_spin)

        row2.addWidget(QLabel("Qualidade máx."))
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(0, 100)
        self.quality_spin.setValue(100)
        self.quality_spin.setToolTip("Mostra grupos que contenham alguma foto com qualidade até este valor")
        row2.addWidget(self.quality_spin)

        row2.addWidget(QLabel("Tamanho mín."))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(0, 5_000_000)
        self.size_spin.setSuffix(" KB")
        row2.addWidget(self.size_spin)

        self.exif_combo = QComboBox()
        self.exif_combo.addItems(["EXIF: tanto faz", "Somente com EXIF", "Somente sem EXIF"])
        row2.addWidget(self.exif_combo)

        self.advanced.setVisible(False)
        card.layout().addWidget(self.advanced)
        return card

    def _build_footer(self) -> Card:
        card = Card(self, margins=(14, 10, 14, 10), spacing=8)
        row = QHBoxLayout()
        self.selection_label = QLabel("Nenhum arquivo marcado para remoção.")
        self.selection_label.setStyleSheet("font-weight: 600;")
        row.addWidget(self.selection_label, 1)

        accept_all = QPushButton("Aceitar recomendações dos grupos visíveis")
        accept_all.clicked.connect(self._accept_visible)
        row.addWidget(accept_all)

        clear = QPushButton("Desmarcar tudo")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self._clear_all_choices)
        row.addWidget(clear)

        self.remove_button = QPushButton("Revisar e remover marcados")
        self.remove_button.setObjectName("Danger")
        self.remove_button.clicked.connect(self.removalRequested.emit)
        row.addWidget(self.remove_button)
        card.layout().addLayout(row)
        return card

    # ---------------------------------------------------------------- dados
    def current_categories(self) -> list[str]:
        return CATEGORY_TABS[self.category_combo.currentIndex()][1]

    def current_order(self) -> str:
        return ORDER_OPTIONS[self.order_combo.currentIndex()][1]

    def current_filters(self) -> dict:
        filters: dict = {}
        text = self.search_edit.text().strip()
        if text:
            filters["text"] = text
        if self.advanced.isVisible():
            if self.format_combo.currentIndex() > 0:
                filters["formats"] = [self.format_combo.currentText()]
            if self.folder_combo.currentIndex() > 0:
                filters["folder"] = self.folder_combo.currentText()
            if self.similarity_spin.value() > 0:
                filters["min_similarity"] = self.similarity_spin.value()
            if self.quality_spin.value() < 100:
                filters["max_quality"] = self.quality_spin.value()
            if self.size_spin.value() > 0:
                filters["min_size"] = self.size_spin.value() * 1024
            if self.exif_combo.currentIndex() == 1:
                filters["has_exif"] = True
            elif self.exif_combo.currentIndex() == 2:
                filters["has_exif"] = False
        return filters

    def refresh_choices(self) -> None:
        """Recarrega as listas de formato/pasta a partir do banco."""
        current_format = self.format_combo.currentText()
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self.format_combo.addItem("Qualquer")
        for fmt in self.repo.distinct_formats():
            self.format_combo.addItem(fmt)
        index = self.format_combo.findText(current_format)
        self.format_combo.setCurrentIndex(max(0, index))
        self.format_combo.blockSignals(False)

        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        self.folder_combo.addItem("Todas")
        for folder in self.repo.distinct_folders():
            self.folder_combo.addItem(folder)
        self.folder_combo.blockSignals(False)

    def reload(self) -> None:
        self._clear_cards()
        self._offset = 0
        categories = self.current_categories()
        filters = self.current_filters()
        self._total = self.repo.count_groups(categories or None, filters=filters)
        summary = self.repo.summary()
        self.summary_label.setText(
            f"{number(self._total)} grupos no filtro atual   ·   "
            f"{number(summary['groups'])} grupos no total   ·   "
            f"até {format_bytes(summary['redundant_bytes'])} em cópias"
        )
        self.load_more()
        self.update_selection_label()

    def load_more(self) -> None:
        categories = self.current_categories()
        groups = self.repo.load_groups(
            categories=categories or None,
            limit=PAGE_SIZE,
            offset=self._offset,
            order=self.current_order(),
            filters=self.current_filters(),
        )
        self._offset += len(groups)
        for group in groups:
            self._add_card(group)
        self.more_button.setVisible(self._offset < self._total)
        self.more_button.setText(
            f"Carregar mais grupos ({number(self._total - self._offset)} restantes)"
        )
        self.empty_label.setVisible(self._total == 0)

    def _add_card(self, group) -> None:
        card = GroupCard(group, self._theme, self.technical_check.isChecked())
        card.choiceChanged.connect(self._on_choice)
        card.referenceChanged.connect(self._on_reference)
        card.statusChanged.connect(self._on_status)
        card.notDuplicates.connect(self._on_not_duplicates)
        card.compareRequested.connect(self._on_compare)
        card.detailsRequested.connect(self.detailsRequested.emit)
        self.container_layout.insertWidget(self.container_layout.count() - 1, card)
        self._cards.append(card)
        for member in group.members:
            file_id = member.signature.file_id
            self._cards_by_file.setdefault(file_id, []).append(card)
            pixmap = self.thumbnails.request(
                file_id, member.signature.thumb, member.signature.path, self.settings.thumbnail_size
            )
            if pixmap is not None:
                card.set_pixmap(file_id, pixmap)

    def _clear_cards(self) -> None:
        for card in self._cards:
            self.container_layout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._cards_by_file.clear()

    # --------------------------------------------------------------- ações
    def _on_thumbnail(self, file_id: int, pixmap) -> None:
        for card in self._cards_by_file.get(file_id, []):
            card.set_pixmap(file_id, pixmap)

    def _on_choice(self, group_id: int, file_id: int, choice: str) -> None:
        self.repo.set_member_choice(group_id, file_id, choice)
        self.repo.refresh_group_totals(group_id)
        for card in self._cards:
            if card.group.group_id == group_id:
                card.refresh_summary()
        self.update_selection_label()

    def _on_reference(self, group_id: int, file_id: int) -> None:
        self.repo.set_reference(group_id, file_id)
        self.statusMessage.emit("Foto principal do grupo atualizada.")
        self._reload_single(group_id)

    def _on_status(self, group_id: int, status: str) -> None:
        self.repo.set_group_status(group_id, status)
        if status == "ignored":
            self._remove_card(group_id)
            self.statusMessage.emit(f"Grupo {group_id} ignorado.")
        self.update_selection_label()

    def _on_not_duplicates(self, group_id: int) -> None:
        file_ids = self.repo.group_files(group_id)
        answer = QMessageBox.question(
            self,
            "Marcar como “não são duplicatas”",
            "Estas fotos deixarão de ser agrupadas nas próximas análises.\n\nDeseja continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        pairs = self.repo.mark_not_duplicates(file_ids)
        self.repo.set_group_status(group_id, "ignored")
        self._remove_card(group_id)
        self.statusMessage.emit(f"Decisão registrada ({pairs} combinações não serão mais sugeridas).")

    def _on_compare(self, group_id: int, file_id: int) -> None:
        group = next((c.group for c in self._cards if c.group.group_id == group_id), None)
        if group is None:
            return
        ordered = [m.signature.file_id for m in group.members]
        if file_id:
            reference = group.reference
            ids = [reference.signature.file_id if reference else ordered[0]]
            if file_id not in ids:
                ids.append(file_id)
        else:
            ids = ordered[:4]
        self.compareRequested.emit(ids)

    def _remove_card(self, group_id: int) -> None:
        for card in list(self._cards):
            if card.group.group_id == group_id:
                self.container_layout.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
                self._cards.remove(card)

    def _reload_single(self, group_id: int) -> None:
        target = self.repo.load_group(group_id)
        if target is None:
            return
        for index, card in enumerate(self._cards):
            if card.group.group_id == group_id:
                position = self.container_layout.indexOf(card)
                self.container_layout.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
                new_card = GroupCard(target, self._theme, self.technical_check.isChecked())
                new_card.choiceChanged.connect(self._on_choice)
                new_card.referenceChanged.connect(self._on_reference)
                new_card.statusChanged.connect(self._on_status)
                new_card.notDuplicates.connect(self._on_not_duplicates)
                new_card.compareRequested.connect(self._on_compare)
                new_card.detailsRequested.connect(self.detailsRequested.emit)
                self.container_layout.insertWidget(position, new_card)
                self._cards[index] = new_card
                for member in target.members:
                    fid = member.signature.file_id
                    self._cards_by_file.setdefault(fid, []).append(new_card)
                    pixmap = self.thumbnails.request(
                        fid, member.signature.thumb, member.signature.path, self.settings.thumbnail_size
                    )
                    if pixmap is not None:
                        new_card.set_pixmap(fid, pixmap)
                break
        self.update_selection_label()

    def _accept_visible(self) -> None:
        for card in self._cards:
            if card.group.category in (Category.EXACT, Category.VISUAL):
                card.accept_recommendation()
                for member in card.group.members:
                    self.repo.set_member_choice(
                        card.group.group_id, member.signature.file_id, member.effective_choice
                    )
                self.repo.refresh_group_totals(card.group.group_id)
        self.update_selection_label()
        self.statusMessage.emit("Recomendações aplicadas aos grupos visíveis.")

    def _clear_all_choices(self) -> None:
        answer = QMessageBox.question(
            self,
            "Desmarcar tudo",
            "Isto remove todas as marcações de remoção feitas até agora (nenhum arquivo é alterado).\n\nContinuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for card in self._cards:
            card.keep_all()
        with self.repo.db.lock:
            self.repo.db.connection.execute("UPDATE group_members SET user_choice='keep'")
            self.repo.db.connection.execute("UPDATE groups SET wasted_bytes=0")
            self.repo.db.connection.commit()
        self.update_selection_label()

    def _toggle_advanced(self, visible: bool) -> None:
        self.advanced.setVisible(visible)
        if not visible:
            self.reload()

    def _toggle_technical(self, visible: bool) -> None:
        self.settings.show_technical_details = visible
        self.settings.save()
        for card in self._cards:
            card.set_technical_visible(visible)

    def update_selection_label(self) -> None:
        selected = self.repo.selected_for_removal()
        total_bytes = sum(int(item["size"] or 0) for item in selected)
        if selected:
            self.selection_label.setText(
                f"{number(len(selected))} arquivo(s) marcados para remoção  ·  {format_bytes(total_bytes)} a liberar"
            )
            self.selection_label.setStyleSheet(f"color: {self._colors['warning']}; font-weight: 700;")
        else:
            self.selection_label.setText("Nenhum arquivo marcado para remoção.")
            self.selection_label.setStyleSheet("font-weight: 600;")
        self.remove_button.setEnabled(bool(selected))
