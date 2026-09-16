"""Pontes entre o núcleo (que roda em threads/processos) e a interface Qt.

Regra de ouro do Qt: widgets só podem ser tocados na thread da interface. Por
isso todo trabalho pesado acontece aqui dentro, e o resultado volta por sinais.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from ..config import Settings
from ..core import imaging
from ..core.fileops import DeletionPlan, FileManager
from ..core.pipeline import AnalysisPipeline, PipelineCallbacks, regroup_only
from ..db.repository import Repository

log = logging.getLogger(__name__)


class AnalysisWorker(QThread):
    """Executa o :class:`AnalysisPipeline` fora da thread da interface."""

    statsChanged = Signal(object)
    phaseChanged = Signal(str, str)
    logMessage = Signal(str)
    failed = Signal(str)
    completed = Signal(object)

    def __init__(self, repo: Repository, settings: Settings, folders: list[str], parent=None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self.folders = folders
        self.pipeline = AnalysisPipeline(
            repo,
            settings,
            PipelineCallbacks(
                on_stats=self.statsChanged.emit,
                on_phase=lambda phase, message: self.phaseChanged.emit(phase, message),
                on_log=self.logMessage.emit,
                on_error=self.failed.emit,
            ),
        )

    def run(self) -> None:  # noqa: D102 - QThread
        stats = self.pipeline.run(self.folders)
        self.completed.emit(stats)

    # Controles repassados ao pipeline -----------------------------------
    def pause(self) -> None:
        self.pipeline.pause()

    def resume(self) -> None:
        self.pipeline.resume()

    def cancel(self) -> None:
        self.pipeline.cancel()

    @property
    def paused(self) -> bool:
        return self.pipeline.is_paused


class RegroupWorker(QThread):
    """Refaz apenas o agrupamento (quando os limites mudam nas configurações)."""

    completed = Signal(int)
    failed = Signal(str)

    def __init__(self, repo: Repository, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            self.completed.emit(regroup_only(self.repo, self.settings))
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha ao reagrupar")
            self.failed.emit(str(exc))


class FileOperationWorker(QThread):
    """Executa (ou desfaz) um plano de remoção já confirmado pelo usuário."""

    progress = Signal(int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        manager: FileManager,
        plan: DeletionPlan | None = None,
        undo_batch: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.plan = plan
        self.undo_batch = undo_batch

    def run(self) -> None:  # noqa: D102
        try:
            if self.undo_batch:
                result = self.manager.undo(self.undo_batch, progress=self.progress.emit)
            else:
                assert self.plan is not None
                result = self.manager.execute(self.plan, confirmed=True, progress=self.progress.emit)
            self.completed.emit(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha na operação de arquivos")
            self.failed.emit(str(exc))


# --------------------------------------------------------------------- miniaturas
class _ThumbSignals(QObject):
    done = Signal(int, object)


class _ThumbTask(QRunnable):
    """Carrega (ou gera) a miniatura de uma foto em segundo plano."""

    def __init__(self, file_id: int, thumb_path: str, source_path: str, size: int, signals: _ThumbSignals) -> None:
        super().__init__()
        self.file_id = file_id
        self.thumb_path = thumb_path
        self.source_path = source_path
        self.size = size
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D102
        image = QImage()
        try:
            if self.thumb_path and Path(self.thumb_path).exists():
                image.load(self.thumb_path)
            if image.isNull() and self.source_path and Path(self.source_path).exists():
                data = imaging.make_thumbnail_bytes(self.source_path, self.size)
                if data:
                    image.loadFromData(data, "JPEG")
        except Exception:  # noqa: BLE001 - miniatura nunca derruba a interface
            log.debug("Falha ao carregar miniatura de %s", self.source_path, exc_info=True)
        self.signals.done.emit(self.file_id, image)


class ThumbnailLoader(QObject):
    """Carregador assíncrono com cache, compartilhado por toda a interface."""

    loaded = Signal(int, QPixmap)

    def __init__(self, max_cache: int = 900, parent=None) -> None:
        super().__init__(parent)
        self._cache: dict[int, QPixmap] = {}
        self._order: list[int] = []
        self._pending: set[int] = set()
        self._max = max_cache
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, min(4, QThreadPool.globalInstance().maxThreadCount())))
        self._signals = _ThumbSignals()
        self._signals.done.connect(self._on_done)

    def get(self, file_id: int) -> QPixmap | None:
        pixmap = self._cache.get(file_id)
        if pixmap is not None:
            try:
                self._order.remove(file_id)
            except ValueError:
                pass
            self._order.append(file_id)
        return pixmap

    def request(self, file_id: int, thumb_path: str, source_path: str, size: int = 320) -> QPixmap | None:
        cached = self.get(file_id)
        if cached is not None:
            return cached
        if file_id in self._pending:
            return None
        self._pending.add(file_id)
        self._pool.start(_ThumbTask(file_id, thumb_path, source_path, size, self._signals))
        return None

    def _on_done(self, file_id: int, image: QImage) -> None:
        self._pending.discard(file_id)
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        self._cache[file_id] = pixmap
        self._order.append(file_id)
        while len(self._order) > self._max:
            oldest = self._order.pop(0)
            self._cache.pop(oldest, None)
        self.loaded.emit(file_id, pixmap)

    def clear(self) -> None:
        self._cache.clear()
        self._order.clear()

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)


def scaled(pixmap: QPixmap, size: QSize) -> QPixmap:
    """Redimensiona mantendo proporção, com suavização."""
    if pixmap.isNull():
        return pixmap
    return pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
