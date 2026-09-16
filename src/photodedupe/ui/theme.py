"""Tema visual do aplicativo (claro e escuro) em uma única folha de estilo."""

from __future__ import annotations

DARK = {
    "bg": "#14161a",
    "surface": "#1b1f26",
    "surface2": "#232833",
    "surface3": "#2b313d",
    "border": "#333a47",
    "text": "#e9ecf1",
    "muted": "#98a2b3",
    "accent": "#3b9eff",
    "accent_hover": "#57adff",
    "accent_press": "#2a86e0",
    "success": "#37b26c",
    "warning": "#f5a524",
    "danger": "#e5484d",
    "danger_hover": "#f05257",
    "star": "#ffce3d",
    "selection": "#1f4b7a",
}

LIGHT = {
    "bg": "#f3f5f8",
    "surface": "#ffffff",
    "surface2": "#eef1f6",
    "surface3": "#e2e7ef",
    "border": "#ccd3de",
    "text": "#1a1f29",
    "muted": "#5c6677",
    "accent": "#1f6fd0",
    "accent_hover": "#2b82ea",
    "accent_press": "#17599f",
    "success": "#1f8f52",
    "warning": "#b8751a",
    "danger": "#c62b30",
    "danger_hover": "#d93b40",
    "star": "#c99400",
    "selection": "#cfe2f8",
}

CATEGORY_COLORS = {
    "exact": "#e5484d",
    "visual": "#f76b15",
    "very_similar": "#e0c000",
    "similar": "#3b9eff",
}


def palette(theme: str = "dark") -> dict[str, str]:
    return DARK if theme == "dark" else LIGHT


def stylesheet(theme: str = "dark") -> str:
    """Folha de estilo completa da aplicação."""
    c = palette(theme)
    return f"""
    QWidget {{
        background-color: {c['bg']};
        color: {c['text']};
        font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background-color: {c['bg']}; }}
    QLabel, QCheckBox, QRadioButton {{ background: transparent; }}

    /* ---------------------------------------------------- barra lateral */
    #Sidebar {{
        background-color: {c['surface']};
        border-right: 1px solid {c['border']};
    }}
    #SidebarTitle {{
        font-size: 17px; font-weight: 700; color: {c['text']};
        padding: 18px 16px 4px 16px; background: transparent;
    }}
    #SidebarSubtitle {{
        font-size: 11px; color: {c['muted']}; padding: 0 16px 14px 16px; background: transparent;
    }}
    QPushButton#NavButton {{
        background: transparent; border: none; border-radius: 8px;
        padding: 10px 14px; margin: 2px 10px; text-align: left;
        font-size: 13px; color: {c['muted']};
    }}
    QPushButton#NavButton:hover {{ background-color: {c['surface2']}; color: {c['text']}; }}
    QPushButton#NavButton:checked {{
        background-color: {c['accent']}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#NavButton:disabled {{ color: {c['border']}; }}

    /* --------------------------------------------------------- cartões */
    #Card, QGroupBox {{
        background-color: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 12px;
    }}
    QGroupBox {{ margin-top: 14px; padding: 16px 12px 12px 12px; font-weight: 600; }}
    QGroupBox::title {{
        subcontrol-origin: margin; left: 14px; padding: 0 6px;
        color: {c['muted']}; background-color: {c['surface']};
    }}
    #PhotoCard {{
        background-color: {c['surface2']};
        border: 2px solid {c['border']};
        border-radius: 10px;
    }}
    #PhotoCard[selected="true"] {{ border: 2px solid {c['accent']}; background-color: {c['selection']}; }}
    #PhotoCard[best="true"] {{ border: 2px solid {c['star']}; }}
    #PhotoCard[marked="remove"] {{ border: 2px solid {c['danger']}; }}

    /* ---------------------------------------------------------- textos */
    #Title {{ font-size: 24px; font-weight: 700; }}
    #Subtitle {{ font-size: 13px; color: {c['muted']}; }}
    #SectionTitle {{ font-size: 15px; font-weight: 600; }}
    #Muted, QLabel[muted="true"] {{ color: {c['muted']}; }}
    #Mono {{ font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 11px; }}
    #StatValue {{ font-size: 21px; font-weight: 700; }}
    #StatLabel {{ font-size: 11px; color: {c['muted']}; }}
    #Badge {{
        border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600; color: #ffffff;
    }}

    /* --------------------------------------------------------- botões */
    QPushButton {{
        background-color: {c['surface3']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 8px 16px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background-color: {c['border']}; }}
    QPushButton:pressed {{ background-color: {c['surface2']}; }}
    QPushButton:disabled {{ color: {c['muted']}; background-color: {c['surface2']}; }}
    QPushButton#Primary {{
        background-color: {c['accent']}; border: 1px solid {c['accent']}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background-color: {c['accent_hover']}; }}
    QPushButton#Primary:pressed {{ background-color: {c['accent_press']}; }}
    QPushButton#Primary:disabled {{ background-color: {c['surface3']}; border-color: {c['border']}; color: {c['muted']}; }}
    QPushButton#Danger {{
        background-color: {c['danger']}; border: 1px solid {c['danger']}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#Danger:hover {{ background-color: {c['danger_hover']}; }}
    QPushButton#Ghost {{ background: transparent; border: 1px solid {c['border']}; }}
    QPushButton#Link {{ background: transparent; border: none; color: {c['accent']}; text-decoration: underline; padding: 2px; }}

    /* --------------------------------------------------- campos e listas */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QDateEdit {{
        background-color: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 7px;
        padding: 6px 9px;
        selection-background-color: {c['accent']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {c['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background-color: {c['surface2']}; border: 1px solid {c['border']};
        selection-background-color: {c['accent']}; outline: none;
    }}
    QListWidget, QTreeWidget, QTableWidget {{
        background-color: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        outline: none;
    }}
    QListWidget::item {{ padding: 7px; border-radius: 6px; }}
    QListWidget::item:selected {{ background-color: {c['selection']}; color: {c['text']}; }}
    QHeaderView::section {{
        background-color: {c['surface2']}; border: none; border-bottom: 1px solid {c['border']};
        padding: 7px; font-weight: 600;
    }}

    /* ------------------------------------------------------- progresso */
    QProgressBar {{
        background-color: {c['surface2']};
        border: 1px solid {c['border']};
        border-radius: 9px;
        height: 18px;
        text-align: center;
        font-size: 11px;
        font-weight: 600;
    }}
    QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 8px; }}

    /* ----------------------------------------------------------- abas */
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 10px; top: -1px; }}
    QTabBar::tab {{
        background: transparent; padding: 9px 18px; margin-right: 4px;
        border-top-left-radius: 8px; border-top-right-radius: 8px; color: {c['muted']};
    }}
    QTabBar::tab:selected {{ background-color: {c['surface']}; color: {c['text']}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}

    /* ------------------------------------------------------- diversos */
    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; }}
    QCheckBox::indicator {{
        border: 1px solid {c['border']}; border-radius: 5px; background-color: {c['surface2']};
    }}
    QCheckBox::indicator:checked {{ background-color: {c['accent']}; border-color: {c['accent']}; }}
    QSlider::groove:horizontal {{ height: 5px; background: {c['surface3']}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {c['accent']}; width: 15px; height: 15px; margin: -6px 0; border-radius: 8px;
    }}
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['surface3']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['border']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['surface3']}; border-radius: 5px; min-width: 30px; }}
    QScrollArea {{ border: none; background: transparent; }}
    QSplitter::handle {{ background-color: {c['border']}; }}
    QToolTip {{
        background-color: {c['surface3']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px; padding: 6px;
    }}
    QStatusBar {{ background-color: {c['surface']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
    QMenu {{ background-color: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
    QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background-color: {c['accent']}; color: #ffffff; }}
    """


def quality_color(score: float, theme: str = "dark") -> str:
    c = palette(theme)
    if score >= 80:
        return c["success"]
    if score >= 60:
        return c["accent"]
    if score >= 40:
        return c["warning"]
    return c["danger"]
