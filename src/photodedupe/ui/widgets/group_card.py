"""Cartão de um grupo de duplicatas: cabeçalho, fotos e ações do grupo."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.models import Category, Group
from ..theme import palette
from .common import Card, category_badge, decimal, human_size, number
from .flow_layout import FlowLayout
from .photo_card import PhotoCard


class GroupCard(Card):
    """Um grupo completo, com todas as fotos e os botões de decisão."""

    choiceChanged = Signal(int, int, str)     # group_id, file_id, escolha
    referenceChanged = Signal(int, int)       # group_id, file_id
    statusChanged = Signal(int, str)          # group_id, status
    notDuplicates = Signal(int)               # group_id
    compareRequested = Signal(int, int)       # group_id, file_id (0 = todas)
    detailsRequested = Signal(int)            # file_id
    thumbnailNeeded = Signal(int, str, str)   # file_id, caminho da miniatura, caminho original

    def __init__(self, group: Group, theme: str = "dark", show_technical: bool = False, parent=None):
        super().__init__(parent, margins=(16, 14, 16, 14), spacing=10)
        self.group = group
        self._theme = theme
        self._colors = palette(theme)
        self.cards: dict[int, PhotoCard] = {}

        self._layout.addWidget(self._build_header())

        photos = QWidget()
        self.flow = FlowLayout(photos, margin=0, spacing=10)
        for member in group.members:
            card = PhotoCard(member, theme, show_technical)
            card.choiceChanged.connect(lambda fid, choice: self.choiceChanged.emit(self.group.group_id, fid, choice))
            card.makeReference.connect(lambda fid: self.referenceChanged.emit(self.group.group_id, fid))
            card.openRequested.connect(lambda fid: self.compareRequested.emit(self.group.group_id, fid))
            card.detailsRequested.connect(self.detailsRequested.emit)
            self.flow.addWidget(card)
            self.cards[member.signature.file_id] = card
        self._layout.addWidget(photos)

        self._layout.addWidget(self._build_actions())
        self.refresh_summary()

    # ------------------------------------------------------------ montagem
    def _build_header(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        title = QLabel(f"Grupo {self.group.group_id:03d}")
        title.setObjectName("SectionTitle")
        row.addWidget(title)
        row.addWidget(category_badge(self.group.category))

        self.count_label = QLabel()
        self.count_label.setObjectName("Muted")
        row.addWidget(self.count_label)
        row.addStretch(1)

        self.savings_label = QLabel()
        self.savings_label.setStyleSheet(f"color: {self._colors['success']}; font-weight: 600;")
        row.addWidget(self.savings_label)
        return holder

    def _build_actions(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        explanation = QLabel(self._explanation())
        explanation.setObjectName("StatLabel")
        explanation.setWordWrap(True)
        row.addWidget(explanation, 1)

        compare = QPushButton("Comparar lado a lado")
        compare.setObjectName("Ghost")
        compare.clicked.connect(lambda: self.compareRequested.emit(self.group.group_id, 0))
        row.addWidget(compare)

        accept = QPushButton("Aceitar recomendação")
        accept.setObjectName("Primary")
        accept.setToolTip("Marca para remoção todas as cópias, mantendo a foto recomendada")
        accept.clicked.connect(self.accept_recommendation)
        row.addWidget(accept)

        keep_all = QPushButton("Manter todas")
        keep_all.setObjectName("Ghost")
        keep_all.clicked.connect(self.keep_all)
        row.addWidget(keep_all)

        not_dup = QPushButton("Não são duplicatas")
        not_dup.setObjectName("Ghost")
        not_dup.setToolTip("O aplicativo deixa de sugerir este agrupamento nas próximas análises")
        not_dup.clicked.connect(lambda: self.notDuplicates.emit(self.group.group_id))
        row.addWidget(not_dup)

        ignore = QPushButton("Ignorar grupo")
        ignore.setObjectName("Ghost")
        ignore.clicked.connect(lambda: self.statusChanged.emit(self.group.group_id, "ignored"))
        row.addWidget(ignore)
        return holder

    def _explanation(self) -> str:
        if self.group.category is Category.EXACT:
            return "Arquivos idênticos byte a byte: remover as cópias não causa perda nenhuma."
        if self.group.category is Category.VISUAL:
            return "É a mesma fotografia salva de formas diferentes. A versão recomendada é a de melhor qualidade técnica."
        if self.group.category is Category.VERY_SIMILAR:
            return "Imagens quase iguais. Confira antes de remover: pode haver diferenças importantes (recorte, edição, momento da foto)."
        return "Imagens parecidas, provavelmente fotos diferentes. Nada é sugerido para remoção aqui."

    # -------------------------------------------------------------- ações
    def accept_recommendation(self) -> None:
        for file_id, card in self.cards.items():
            if card.member.is_reference:
                continue
            if card.member.recommendation == "remove":
                card.remove_radio.setChecked(True)
            else:
                card.keep_radio.setChecked(True)
        self.statusChanged.emit(self.group.group_id, "reviewed")
        self.refresh_summary()

    def keep_all(self) -> None:
        for card in self.cards.values():
            card.keep_radio.setChecked(True)
        self.statusChanged.emit(self.group.group_id, "reviewed")
        self.refresh_summary()

    def mark_all_but_reference(self) -> None:
        self.accept_recommendation()

    def set_technical_visible(self, visible: bool) -> None:
        for card in self.cards.values():
            card.set_technical_visible(visible)

    def set_pixmap(self, file_id: int, pixmap) -> None:
        card = self.cards.get(file_id)
        if card is not None:
            card.set_pixmap(pixmap)

    def request_thumbnails(self, thumbs: dict[int, tuple[str, str]]) -> None:
        for file_id, (thumb_path, source) in thumbs.items():
            if file_id in self.cards:
                self.thumbnailNeeded.emit(file_id, thumb_path, source)

    def refresh_summary(self) -> None:
        marked = sum(1 for c in self.cards.values() if c.member.effective_choice == "remove")
        freed = sum(c.member.signature.size for c in self.cards.values() if c.member.effective_choice == "remove")
        self.count_label.setText(
            f"{number(self.group.size)} versões encontradas"
            + (f"  ·  {number(marked)} marcadas para remoção" if marked else "")
        )
        if freed:
            self.savings_label.setText(f"Liberará {human_size(freed)}")
        else:
            similarity = self.group.min_similarity
            self.savings_label.setText(f"Semelhança mínima: {decimal(similarity, 1)}%")
        for card in self.cards.values():
            card._refresh_state()
