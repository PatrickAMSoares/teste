# -*- mode: python ; coding: utf-8 -*-
"""Especificação do PyInstaller para gerar o PhotoDedupe.exe.

Gera uma pasta (``onedir``) com o executável e as bibliotecas ao lado - o modo
mais rápido para abrir e o mais simples de empacotar no instalador.

Uso::

    pyinstaller packaging/photodedupe.spec --noconfirm --clean
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJETO = Path(SPECPATH).resolve().parent
RECURSOS = PROJETO / "src" / "photodedupe" / "resources"

datas = [(str(RECURSOS), "photodedupe/resources")]
hiddenimports = [
    "photodedupe",
    "photodedupe.app",
    "photodedupe.core.analyzer",       # carregado nos processos de trabalho
    "photodedupe.core.pipeline",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PIL._tkinter_finder",
]

# Dependências opcionais: entram no pacote quando estiverem instaladas.
#
# O OpenCV acelera a análise em cerca de 17%, mas soma ~150 MB ao pacote.
# Defina PHOTODEDUPE_SKIP_CV2=1 (ou use -SemOpenCV no build_windows.ps1) para
# deixá-lo de fora: o aplicativo continua funcionando com o caminho NumPy.
opcionais = ["pillow_heif", "rawpy", "send2trash", "openpyxl", "reportlab"]
if os.environ.get("PHOTODEDUPE_SKIP_CV2", "") not in ("1", "true", "True"):
    opcionais.append("cv2")
else:
    excludes_cv2 = True

for modulo in opcionais:
    try:
        __import__(modulo)
    except Exception:  # noqa: BLE001
        continue
    hiddenimports.append(modulo)
    try:
        datas += collect_data_files(modulo)
        hiddenimports += collect_submodules(modulo)
    except Exception:  # noqa: BLE001
        pass

# Módulos grandes que o aplicativo não usa - manter fora reduz muito o tamanho.
excludes = ([] if "cv2" in opcionais else ["cv2"]) + [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth",
    "PySide6.QtNetworkAuth", "PySide6.QtPositioning", "PySide6.QtSerialPort",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp",
]

a = Analysis(
    [str(PROJETO / "packaging" / "launcher.py")],
    pathex=[str(PROJETO / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoDedupe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # aplicativo de janela, sem console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(RECURSOS / "photodedupe.ico"),
    version=str(PROJETO / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhotoDedupe",
)
