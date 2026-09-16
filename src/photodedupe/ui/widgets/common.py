"""Widgets pequenos reaproveitados em várias telas."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.fileops import format_bytes
from ..theme import CATEGORY_COLORS, quality_color


def number(value: float | int) -> str:
    """Formata números no padrão brasileiro (1.234.567)."""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


def decimal(value: float, casas: int = 1) -> str:
    return f"{value:.{casas}f}".replace(".", ",")


def human_size(value: float) -> str:
    return format_bytes(value)


class Card(QFrame):
    """Painel com fundo e borda arredondada."""

    def __init__(self, parent=None, margins: tuple[int, int, int, int] = (16, 16, 16, 16), spacing: int = 10):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(spacing)

    def layout(self) -> QVBoxLayout:  # type: ignore[override]
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget


class StatTile(Card):
    """Bloco “valor + rótulo” usado nos painéis de resumo."""

    def __init__(self, label: str, value: str = "0", hint: str = "", accent: str | None = None, parent=None):
        super().__init__(parent, margins=(14, 12, 14, 12), spacing=2)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        if accent:
            self.value_label.setStyleSheet(f"color: {accent};")
        self.text_label = QLabel(label)
        self.text_label.setObjectName("StatLabel")
        self.text_label.setWordWrap(True)
        self._layout.addWidget(self.value_label)
        self._layout.addWidget(self.text_label)
        if hint:
            self.setToolTip(hint)
        self.setMinimumWidth(130)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class Badge(QLabel):
    """Etiqueta colorida (categoria, aviso, situação)."""

    def __init__(self, text: str, color: str = "#3b9eff", parent=None):
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.set_color(color)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background-color: {color}; color: #ffffff; border-radius: 9px;"
            "padding: 2px 9px; font-size: 11px; font-weight: 600;"
        )


def category_badge(category) -> Badge:
    color = CATEGORY_COLORS.get(getattr(category, "value", str(category)), "#3b9eff")
    return Badge(f"{category.emoji} {category.label}", color)


class QualityBar(QWidget):
    """Barra de qualidade 0-100 com o número ao lado."""

    def __init__(self, score: float = 0.0, theme: str = "dark", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(7)
        self.label = QLabel()
        self.label.setObjectName("StatLabel")
        self.label.setFixedWidth(48)
        layout.addWidget(self.bar, 1)
        layout.addWidget(self.label)
        self._theme = theme
        self.set_score(score)

    def set_score(self, score: float) -> None:
        color = quality_color(score, self._theme)
        self.bar.setValue(int(round(score)))
        self.bar.setStyleSheet(
            f"QProgressBar {{ background: rgba(128,128,128,0.25); border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}"
        )
        self.label.setText(f"{score:.0f}/100")
        self.label.setStyleSheet(f"color: {color}; font-weight: 600;")


class ElidedLabel(QLabel):
    """Rótulo que corta o texto no meio quando não cabe (bom para caminhos)."""

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setText(text)
        self.setToolTip(text)

    def setFullText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._apply()

    def resizeEvent(self, event):  # noqa: D102, N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, self._mode, max(40, self.width() - 4)))


class Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFixedHeight(1)
        self.setStyleSheet("background-color: rgba(128,128,128,0.25); border: none;")


def title_block(title: str, subtitle: str = "") -> QWidget:
    """Cabeçalho padrão das páginas."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    label = QLabel(title)
    label.setObjectName("Title")
    layout.addWidget(label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("Subtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return holder
