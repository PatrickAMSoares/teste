"""Ponto de entrada usado pelo PyInstaller para gerar o executável do Windows.

O ``freeze_support`` precisa ser a primeira coisa executada: sem ele, cada
processo de trabalho criado pelo ``multiprocessing`` reabriria a janela do
aplicativo em vez de analisar fotos.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    multiprocessing.freeze_support()

    if getattr(sys, "frozen", False):
        # No executável empacotado, o Qt fica junto do binário.
        base = os.path.dirname(sys.executable)
        os.environ.setdefault("QT_PLUGIN_PATH", os.path.join(base, "PySide6", "plugins"))

    from photodedupe.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
