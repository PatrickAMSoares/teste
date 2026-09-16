"""Descoberta dos diretórios usados pelo aplicativo (dados, cache, logs).

Tudo fica dentro do perfil do usuário. Nada é gravado junto das fotos.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

_ENV_OVERRIDE = "PHOTODEDUPE_HOME"


def app_home() -> Path:
    """Diretório raiz de dados do aplicativo."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_NAME.lower()


def data_dir() -> Path:
    return _ensure(app_home())


def db_path() -> Path:
    return data_dir() / "photodedupe.db"


def thumbs_dir() -> Path:
    return _ensure(app_home() / "thumbnails")


def logs_dir() -> Path:
    return _ensure(app_home() / "logs")


def reports_dir() -> Path:
    return _ensure(app_home() / "relatorios")


def default_quarantine_dir() -> Path:
    return app_home() / "quarentena"


def models_dir() -> Path:
    """Local onde modelos de IA locais (ONNX) podem ser colocados pelo usuário."""
    return _ensure(app_home() / "modelos")


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(relative: str) -> Path:
    """Resolve arquivos de recurso tanto em execução normal quanto empacotada (PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "photodedupe" / "resources" / relative
        if candidate.exists():
            return candidate
        candidate = Path(base) / relative
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "resources" / relative
