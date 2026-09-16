"""Tela “Revisar recomendações”: um grupo por vez, decisão rápida."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
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


class ReviewPage(QWidget):
    """Apresenta um grupo de cada vez com as ações: aceitar, rejeitar, ignorar."""

    statusMessage = Signal(str)
    removalRequested = Signal()
    compareRequested = Signal(list)
    detailsRequested = Signal(int)

    def __init__(self, repo: Repository, settings: Settings, thumbnails, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self.thumbnails = thumbnails
        self._theme = theme
        self._colors = palette(theme)
        self._queue: list[int] = []
        self._position = 0
        self._card: GroupCard | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)
        layout.addWidget(
            title_block(
                "Revisar recomendações",
                "Um grupo por vez. O aplicativo sugere o que manter; a decisão final é sempre sua.",
            )
        )

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Subtitle")
        layout.addWidget(self.progress_label)

        self.suggestion = Card(self, margins=(16, 12, 16, 12), spacing=4)
        self.suggestion_title = QLabel("")
        self.suggestion_title.setObjectName("SectionTitle")
        self.suggestion_detail = QLabel("")
        self.suggestion_detail.setObjectName("StatLabel")
        self.suggestion_detail.setWordWrap(True)
        self.suggestion.layout().addWidget(self.suggestion_title)
        self.suggestion.layout().addWidget(self.suggestion_detail)
        layout.addWidget(self.suggestion)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QWidget()
        self.holder_layout = QVBoxLayout(self.holder)
        self.holder_layout.setContentsMargins(0, 0, 8, 0)
        self.holder_layout.addStretch(1)
        self.scroll.setWidget(self.holder)
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("Nada para revisar. Faça uma análise ou ajuste os filtros na tela de resultados.")
        self.empty_label.setObjectName("Subtitle")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        layout.addLayout(self._build_buttons())
        self.thumbnails.loaded.connect(self._on_thumbnail)

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.previous_button = QPushButton("←  Anterior")
        self.previous_button.clicked.connect(self.previous_group)
        row.addWidget(self.previous_button)
        row.addStretch(1)

        self.reject_button = QPushButton("Manter todas")
        self.reject_button.clicked.connect(self._keep_all)
        row.addWidget(self.reject_button)

        self.not_dup_button = QPushButton("Não são duplicatas")
        self.not_dup_button.setObjectName("Ghost")
        self.not_dup_button.clicked.connect(self._not_duplicates)
        row.addWidget(self.not_dup_button)

        self.ignore_button = QPushButton("Ignorar grupo")
        self.ignore_button.setObjectName("Ghost")
        self.ignore_button.clicked.connect(self._ignore)
        row.addWidget(self.ignore_button)

        self.accept_button = QPushButton("Aceitar recomendação  →")
        self.accept_button.setObjectName("Primary")
        self.accept_button.clicked.connect(self._accept)
        row.addWidget(self.accept_button)

        self.finish_button = QPushButton("Concluir e remover marcados")
        self.finish_button.setObjectName("Danger")
        self.finish_button.clicked.connect(self.removalRequested.emit)
        row.addWidget(self.finish_button)
        return row

    # ---------------------------------------------------------------- dados
    def reload(self) -> None:
        groups = self.repo.load_groups(
            categories=[Category.EXACT.value, Category.VISUAL.value, Category.VERY_SIMILAR.value],
            statuses=["pending"],
            order="wasted_desc",
            limit=5000,
        )
        self._queue = [g.group_id for g in groups]
        self._position = 0
        self._show_current()

    def _show_current(self) -> None:
        if self._card is not None:
            self.holder_layout.removeWidget(self._card)
            self._card.setParent(None)
            self._card.deleteLater()
            self._card = None

        has_items = 0 <= self._position < len(self._queue)
        self.empty_label.setVisible(not has_items)
        self.suggestion.setVisible(has_items)
        for button in (self.accept_button, self.reject_button, self.ignore_button, self.not_dup_button):
            button.setEnabled(has_items)
        self.previous_button.setEnabled(self._position > 0)
        if not has_items:
            self.progress_label.setText("")
            return

        group = self.repo.load_group(self._queue[self._position])
        if group is None:
            self._advance()
            return

        self.progress_label.setText(
            f"Grupo {number(self._position + 1)} de {number(len(self._queue))} a revisar"
        )
        reference = group.reference
        to_remove = [m for m in group.members if m.recommendation == "remove"]
        freed = sum(m.signature.size for m in to_remove)
        self.suggestion_title.setText(
            f"Manter “{reference.signature.name}”" if reference else "Revisar este grupo"
        )
        if to_remove:
            self.suggestion_detail.setText(
                f"Enviar {number(len(to_remove))} versão(ões) para a quarentena — liberando {format_bytes(freed)}. "
                f"{reference.reason if reference else ''}"
            )
        else:
            self.suggestion_detail.setText(
                "Nenhuma remoção é sugerida para este grupo. Revise as fotos e decida manualmente."
            )

        card = GroupCard(group, self._theme, self.settings.show_technical_details)
        card.choiceChanged.connect(self._on_choice)
        card.referenceChanged.connect(self._on_reference)
        card.statusChanged.connect(lambda gid, status: self.repo.set_group_status(gid, status))
        card.notDuplicates.connect(lambda _gid: self._not_duplicates())
        card.compareRequested.connect(self._on_compare)
        card.detailsRequested.connect(self.detailsRequested.emit)
        self.holder_layout.insertWidget(0, card)
        self._card = card
        for member in group.members:
            pixmap = self.thumbnails.request(
                member.signature.file_id, member.signature.thumb, member.signature.path, self.settings.thumbnail_size
            )
            if pixmap is not None:
                card.set_pixmap(member.signature.file_id, pixmap)

    # --------------------------------------------------------------- ações
    def _current_group_id(self) -> int | None:
        if 0 <= self._position < len(self._queue):
            return self._queue[self._position]
        return None

    def _accept(self) -> None:
        group_id = self._current_group_id()
        if group_id is None or self._card is None:
            return
        self._card.accept_recommendation()
        for member in self._card.group.members:
            self.repo.set_member_choice(group_id, member.signature.file_id, member.effective_choice)
        self.repo.refresh_group_totals(group_id)
        self.repo.set_group_status(group_id, "reviewed")
        self.statusMessage.emit(f"Grupo {group_id}: recomendação aceita.")
        self._advance()

    def _keep_all(self) -> None:
        group_id = self._current_group_id()
        if group_id is None or self._card is None:
            return
        self._card.keep_all()
        for member in self._card.group.members:
            self.repo.set_member_choice(group_id, member.signature.file_id, "keep")
        self.repo.refresh_group_totals(group_id)
        self.repo.set_group_status(group_id, "reviewed")
        self.statusMessage.emit(f"Grupo {group_id}: todas as fotos serão mantidas.")
        self._advance()

    def _ignore(self) -> None:
        group_id = self._current_group_id()
        if group_id is None:
            return
        self.repo.set_group_status(group_id, "ignored")
        self._advance()

    def _not_duplicates(self) -> None:
        group_id = self._current_group_id()
        if group_id is None:
            return
        answer = QMessageBox.question(
            self,
            "Marcar como “não são duplicatas”",
            "Estas fotos deixarão de ser agrupadas nas próximas análises.\n\nDeseja continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.repo.mark_not_duplicates(self.repo.group_files(group_id))
        self.repo.set_group_status(group_id, "ignored")
        self.statusMessage.emit("Decisão registrada: estas fotos não serão mais agrupadas.")
        self._advance()

    def _advance(self) -> None:
        self._position += 1
        self._show_current()

    def previous_group(self) -> None:
        if self._position > 0:
            self._position -= 1
            self._show_current()

    def _on_choice(self, group_id: int, file_id: int, choice: str) -> None:
        self.repo.set_member_choice(group_id, file_id, choice)
        self.repo.refresh_group_totals(group_id)

    def _on_reference(self, group_id: int, file_id: int) -> None:
        self.repo.set_reference(group_id, file_id)
        self._show_current()

    def _on_compare(self, group_id: int, file_id: int) -> None:
        group = self.repo.load_group(group_id)
        if group is None:
            return
        ids = [m.signature.file_id for m in group.members][:4]
        self.compareRequested.emit(ids)

    def _on_thumbnail(self, file_id: int, pixmap) -> None:
        if self._card is not None:
            self._card.set_pixmap(file_id, pixmap)
