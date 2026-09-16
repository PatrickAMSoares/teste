"""Ponto de entrada da interface gráfica."""

from __future__ import annotations

import multiprocessing
import sys

from . import APP_DISPLAY_NAME, APP_NAME, ORG_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    # Necessário para o multiprocessing funcionar no executável do Windows.
    multiprocessing.freeze_support()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .config import Settings
    from .db.database import Database
    from .logging_setup import setup_locale, setup_logging
    from .paths import resource_path
    from .ui.main_window import MainWindow
    from .ui.theme import stylesheet

    log_file = setup_logging()
    setup_locale()

    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)

    icon_path = resource_path("photodedupe.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = Settings.load()
    app.setStyleSheet(stylesheet(settings.theme))

    try:
        db = Database()
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(
            None,
            "Erro ao abrir o banco de dados",
            f"Não foi possível abrir o banco local:\n\n{exc}\n\nRegistro de erros: {log_file}",
        )
        return 1

    window = MainWindow(db, settings)
    window.show()
    try:
        return app.exec()
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
