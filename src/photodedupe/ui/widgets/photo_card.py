"""Cartão de uma foto dentro de um grupo de duplicatas."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
)

from ...core.models import Member
from ...core.quality import quality_label
from ..theme import palette
from .common import ElidedLabel, QualityBar, decimal, human_size

THUMB_SIZE = 210


class PhotoCard(QFrame):
    """Mostra a miniatura, os números principais e a decisão do usuário."""

    choiceChanged = Signal(int, str)        # file_id, "keep"|"remove"
    makeReference = Signal(int)             # file_id
    openRequested = Signal(int)             # file_id (duplo clique -> comparação)
    detailsRequested = Signal(int)          # file_id

    def __init__(self, member: Member, theme: str = "dark", show_technical: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("PhotoCard")
        self.member = member
        self.file_id = member.signature.file_id
        self._theme = theme
        self._colors = palette(theme)
        self.setFixedWidth(THUMB_SIZE + 34)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)

        sig = member.signature

        # ------------------------------------------------------- miniatura
        self.thumb = QLabel()
        self.thumb.setFixedSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            f"background-color: {self._colors['bg']}; border-radius: 8px; color: {self._colors['muted']};"
        )
        self.thumb.setText("carregando...")
        self.thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.thumb)

        # ---------------------------------------------------------- título
        if member.is_reference:
            star = QLabel("⭐  RECOMENDADA PARA MANTER")
            star.setStyleSheet(f"color: {self._colors['star']}; font-weight: 700; font-size: 11px;")
            layout.addWidget(star)

        name = ElidedLabel(sig.name)
        name.setStyleSheet("font-weight: 600;")
        layout.addWidget(name)

        folder = ElidedLabel(str(Path(sig.path).parent))
        folder.setObjectName("StatLabel")
        layout.addWidget(folder)

        info = QLabel(f"{sig.width} × {sig.height}  ·  {human_size(sig.size)}")
        info.setObjectName("StatLabel")
        layout.addWidget(info)

        meta = QLabel(f"{sig.format}  ·  {decimal(sig.megapixels, 1)} MP")
        meta.setObjectName("StatLabel")
        layout.addWidget(meta)

        # ------------------------------------------------------- qualidade
        self.quality_bar = QualityBar(sig.quality, theme)
        self.quality_bar.setToolTip(f"Qualidade técnica: {quality_label(sig.quality)}")
        layout.addWidget(self.quality_bar)

        if not member.is_reference:
            sim = QLabel(f"Semelhança: {decimal(member.similarity, 1)}%")
            sim.setStyleSheet(f"color: {self._colors['accent']}; font-size: 11px; font-weight: 600;")
            layout.addWidget(sim)
        else:
            sim = QLabel("Foto principal do grupo")
            sim.setObjectName("StatLabel")
            layout.addWidget(sim)

        if member.reason:
            reason = QLabel(member.reason)
            reason.setObjectName("StatLabel")
            reason.setWordWrap(True)
            reason.setMaximumHeight(46)
            layout.addWidget(reason)

        # ---------------------------------------------------------- ações
        choice_row = QHBoxLayout()
        choice_row.setSpacing(6)
        self.keep_radio = QRadioButton("Manter")
        self.remove_radio = QRadioButton("Remover")
        self.group_buttons = QButtonGroup(self)
        self.group_buttons.addButton(self.keep_radio)
        self.group_buttons.addButton(self.remove_radio)
        choice = member.effective_choice
        (self.remove_radio if choice == "remove" else self.keep_radio).setChecked(True)
        self.remove_radio.setStyleSheet(f"color: {self._colors['danger']};")
        if member.is_reference:
            self.remove_radio.setEnabled(False)
            self.remove_radio.setToolTip(
                "A foto principal do grupo nunca é removida. Para removê-la, "
                "escolha antes outra foto como principal."
            )
        choice_row.addWidget(self.keep_radio)
        choice_row.addWidget(self.remove_radio)
        choice_row.addStretch(1)
        layout.addLayout(choice_row)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        if not member.is_reference:
            self.star_button = QPushButton("⭐ Principal")
            self.star_button.setObjectName("Ghost")
            self.star_button.setToolTip("Define esta foto como a que será mantida no grupo")
            self.star_button.clicked.connect(lambda: self.makeReference.emit(self.file_id))
            actions.addWidget(self.star_button)
        self.details_button = QPushButton("Detalhes")
        self.details_button.setObjectName("Ghost")
        self.details_button.clicked.connect(lambda: self.detailsRequested.emit(self.file_id))
        actions.addWidget(self.details_button)
        layout.addLayout(actions)

        self.technical = QLabel(self._technical_text())
        self.technical.setObjectName("Mono")
        self.technical.setWordWrap(True)
        self.technical.setVisible(show_technical)
        layout.addWidget(self.technical)

        self.keep_radio.toggled.connect(self._on_choice)
        self._refresh_state()

    # ------------------------------------------------------------- eventos
    def _on_choice(self, _checked: bool) -> None:
        choice = "keep" if self.keep_radio.isChecked() else "remove"
        self.member.user_choice = choice
        self._refresh_state()
        self.choiceChanged.emit(self.file_id, choice)

    def mouseDoubleClickEvent(self, event):  # noqa: D102, N802
        self.openRequested.emit(self.file_id)
        super().mouseDoubleClickEvent(event)

    # -------------------------------------------------------------- visual
    def set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            self.thumb.setText("sem pré-visualização")
            return
        self.thumb.setPixmap(
            pixmap.scaled(
                self.thumb.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_technical_visible(self, visible: bool) -> None:
        self.technical.setVisible(visible)

    def _refresh_state(self) -> None:
        self.setProperty("best", "true" if self.member.is_reference else "false")
        self.setProperty("marked", self.member.effective_choice)
        self.style().unpolish(self)
        self.style().polish(self)

    def _technical_text(self) -> str:
        detail = self.member.detail or {}
        distances = detail.get("distances") or {}
        parts = []
        if distances:
            parts.append(
                "pHash {p} · dHash {d} · wHash {w} · aHash {a}".format(
                    p=distances.get("phash", "-"),
                    d=distances.get("dhash", "-"),
                    w=distances.get("whash", "-"),
                    a=distances.get("ahash", "-"),
                )
            )
        if detail.get("cosine"):
            parts.append(f"cosseno {detail['cosine']:.4f} · correlação {detail.get('ncc', 0):.4f}")
        if detail.get("capped_by"):
            parts.append(f"limitado por: {detail['capped_by']}")
        report = self.member.quality_report
        if report and report.components:
            parts.append(
                "qualidade: "
                + ", ".join(f"{k}={v:.0f}" for k, v in report.components.items())
            )
        if report and report.jpeg_quality_estimate:
            parts.append(f"qualidade JPEG estimada: {report.jpeg_quality_estimate}%")
        return "\n".join(parts) or "sem dados técnicos adicionais"
