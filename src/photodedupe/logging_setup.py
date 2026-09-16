"""Configuração de log em arquivo e console.

Os registros ficam em ``%LOCALAPPDATA%\\PhotoDedupe\\logs`` (Windows). São úteis
para diagnosticar problemas de leitura de arquivos específicos sem expor o
conteúdo das fotos: gravamos apenas caminhos e mensagens de erro.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .paths import logs_dir

_CONFIGURED = False
FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-32s | %(message)s"


def setup_logging(level: int | str | None = None, to_console: bool = True) -> Path:
    """Inicializa o log. Pode ser chamada várias vezes sem efeito colateral."""
    global _CONFIGURED
    log_file = logs_dir() / "photodedupe.log"
    if _CONFIGURED:
        return log_file

    if level is None:
        level = os.environ.get("PHOTODEDUPE_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=4 * 1024 * 1024, backupCount=4, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    if to_console and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        console.setLevel(max(level, logging.INFO))
        root.addHandler(console)

    logging.getLogger("PIL").setLevel(logging.WARNING)
    _CONFIGURED = True
    logging.getLogger(__name__).info("Log iniciado em %s", log_file)
    return log_file


def setup_locale() -> None:
    """Tenta usar o português do Brasil para números e datas."""
    import locale

    for candidate in ("pt_BR.UTF-8", "pt_BR.utf8", "pt_BR", "Portuguese_Brazil.1252", "Portuguese_Brazil", ""):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            return
        except locale.Error:
            continue
