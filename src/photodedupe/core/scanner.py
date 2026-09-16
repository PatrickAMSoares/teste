"""Varredura do sistema de arquivos.

Percorre as pastas escolhidas procurando fotos. Usa ``os.scandir`` (bem mais
rápido que ``os.walk`` com ``stat`` separado) e ignora, por padrão, pastas de
sistema onde não há fotos do usuário. A varredura é somente leitura.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class FileEntry:
    path: str
    size: int
    mtime_ns: int
    folder_id: int | None = None


@dataclass
class ScanOptions:
    extensions: set[str]
    recursive: bool = True
    follow_symlinks: bool = False
    skip_hidden: bool = True
    min_size_bytes: int = 8 * 1024
    excluded_names: tuple[str, ...] = ()


class FolderScanner:
    """Gera :class:`FileEntry` para cada foto encontrada."""

    def __init__(self, options: ScanOptions, cancel_event: threading.Event | None = None) -> None:
        self.opts = options
        self.cancel = cancel_event or threading.Event()
        self.excluded = {name.lower() for name in options.excluded_names}
        self.errors: list[tuple[str, str]] = []
        self.dirs_visited = 0

    def scan(
        self,
        folders: list[str],
        on_progress: Callable[[int, str], None] | None = None,
    ) -> Iterator[FileEntry]:
        seen: set[str] = set()
        found = 0
        for folder in folders:
            root = Path(folder).expanduser()
            if not root.exists():
                self.errors.append((str(root), "pasta não encontrada"))
                continue
            for entry in self._walk(str(root.resolve())):
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                found += 1
                if on_progress is not None and found % 250 == 0:
                    on_progress(found, entry.path)
                yield entry
                if self.cancel.is_set():
                    return
        if on_progress is not None:
            on_progress(found, "")

    def _walk(self, root: str) -> Iterator[FileEntry]:
        stack = [root]
        while stack:
            if self.cancel.is_set():
                return
            current = stack.pop()
            self.dirs_visited += 1
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if self.cancel.is_set():
                            return
                        try:
                            if entry.is_dir(follow_symlinks=self.opts.follow_symlinks):
                                if not self.opts.recursive:
                                    continue
                                name = entry.name
                                if name.lower() in self.excluded:
                                    continue
                                if self.opts.skip_hidden and _is_hidden(entry):
                                    continue
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=self.opts.follow_symlinks):
                                continue
                            suffix = os.path.splitext(entry.name)[1].lower()
                            if suffix not in self.opts.extensions:
                                continue
                            if self.opts.skip_hidden and _is_hidden(entry):
                                continue
                            stat = entry.stat(follow_symlinks=self.opts.follow_symlinks)
                            if stat.st_size < self.opts.min_size_bytes:
                                continue
                            yield FileEntry(entry.path, int(stat.st_size), int(stat.st_mtime_ns))
                        except OSError as exc:
                            self.errors.append((entry.path, str(exc)))
            except PermissionError:
                self.errors.append((current, "sem permissão de acesso"))
            except OSError as exc:
                self.errors.append((current, str(exc)))


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
            return bool(attrs & 0x2) or bool(attrs & 0x4)  # HIDDEN | SYSTEM
        except (AttributeError, OSError):
            return False
    return False


def count_files_estimate(folders: list[str], options: ScanOptions, limit: int = 250_000) -> int:
    """Contagem rápida só para estimar o total (usada na barra de progresso)."""
    scanner = FolderScanner(options)
    total = 0
    for _entry in scanner.scan(folders):
        total += 1
        if total >= limit:
            break
    return total
