"""Orquestração da análise completa.

Fases:

1. **Descoberta** - varre as pastas e registra os arquivos no banco.
2. **Análise** - hash, hashes perceptuais, descritor, EXIF, qualidade e
   miniatura de cada foto, em vários processos.
3. **Agrupamento** - gera candidatos, verifica pares e monta os grupos.

Características importantes:

* **Pausar e retomar**: a pausa interrompe o envio de novos lotes; ao retomar,
  a análise continua de onde parou. Como o progresso fica gravado no banco,
  fechar o aplicativo e abrir de novo também retoma o trabalho.
* **Incremental**: arquivos já analisados e não modificados (mesmo tamanho e
  mesma data de modificação) não são reanalisados.
* **Memória constante**: no máximo ``2 x workers`` fotos decodificadas por vez;
  os resultados vão para o banco em lotes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from ..db.repository import Repository
from .analyzer import AnalyzerOptions, analyze_path
from .grouping import GroupingOptions, build_groups
from .models import ScanStats
from .scanner import FolderScanner, ScanOptions

log = logging.getLogger(__name__)

PHASE_DISCOVER = "descoberta"
PHASE_ANALYZE = "analise"
PHASE_GROUP = "agrupamento"
PHASE_DONE = "concluido"
PHASE_CANCELLED = "cancelado"


@dataclass
class PipelineCallbacks:
    """Ganchos usados pela interface (executados na thread da análise)."""

    on_stats: Callable[[ScanStats], None] | None = None
    on_phase: Callable[[str, str], None] | None = None
    on_log: Callable[[str], None] | None = None
    on_finished: Callable[[ScanStats], None] | None = None
    on_error: Callable[[str], None] | None = None


@dataclass
class _RateMeter:
    """Média móvel para velocidade e tempo restante."""

    window: deque = field(default_factory=lambda: deque(maxlen=40))

    def tick(self, count: int) -> None:
        self.window.append((time.perf_counter(), count))

    def rate(self) -> float:
        if len(self.window) < 2:
            return 0.0
        (t0, c0), (t1, c1) = self.window[0], self.window[-1]
        dt = t1 - t0
        return (c1 - c0) / dt if dt > 0.001 else 0.0


class AnalysisPipeline:
    """Executa a análise completa. Deve rodar em uma thread separada da interface."""

    def __init__(self, repo: Repository, settings: Settings, callbacks: PipelineCallbacks | None = None) -> None:
        self.repo = repo
        self.settings = settings
        self.cb = callbacks or PipelineCallbacks()
        self.stats = ScanStats()
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._running = False
        self._meter = _RateMeter()
        self._session_id: int | None = None
        self._executor: ProcessPoolExecutor | None = None

    # ------------------------------------------------------------- controles
    def pause(self) -> None:
        if self._running:
            self._resume.clear()
            self._emit_phase(self.stats.phase, "Análise pausada")
            self._log("Análise pausada pelo usuário.")

    def resume(self) -> None:
        if self._running:
            self._resume.set()
            self._log("Análise retomada.")

    def cancel(self) -> None:
        self._cancel.set()
        self._resume.set()
        self._log("Cancelando a análise...")

    @property
    def is_paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def is_running(self) -> bool:
        return self._running

    def _wait_if_paused(self) -> None:
        while not self._resume.is_set() and not self._cancel.is_set():
            self._resume.wait(0.2)

    # ------------------------------------------------------------------ fluxo
    def run(self, folders: list[str] | None = None) -> ScanStats:
        """Executa a análise inteira. Bloqueia até terminar (ou ser cancelada)."""
        self._running = True
        self._cancel.clear()
        self._resume.set()
        started = time.perf_counter()
        folder_list = folders if folders is not None else [f["path"] for f in self.repo.list_folders()]
        self._session_id = self.repo.start_session(folder_list)
        try:
            if not folder_list:
                raise ValueError("Nenhuma pasta selecionada para análise.")
            self._discover(folder_list)
            if not self._cancel.is_set():
                self._analyze()
            if not self._cancel.is_set():
                self._group()
            self.stats.elapsed_s = time.perf_counter() - started
            if self._cancel.is_set():
                self.stats.phase = PHASE_CANCELLED
                self._emit_phase(PHASE_CANCELLED, "Análise cancelada")
                self.repo.update_session(self._session_id, "cancelled", self.stats.to_dict())
            else:
                self.stats.phase = PHASE_DONE
                self._emit_phase(PHASE_DONE, "Análise concluída")
                self.repo.update_session(self._session_id, "done", self.stats.to_dict())
            self._emit_stats()
            if self.cb.on_finished:
                self.cb.on_finished(self.stats)
            return self.stats
        except Exception as exc:  # noqa: BLE001 - a interface precisa saber do erro
            log.exception("Falha na análise")
            if self._session_id:
                self.repo.update_session(self._session_id, "error", self.stats.to_dict())
            if self.cb.on_error:
                self.cb.on_error(str(exc))
            self.stats.phase = "erro"
            if self.cb.on_finished:
                self.cb.on_finished(self.stats)
            return self.stats
        finally:
            self._running = False
            self._shutdown_executor(cancel=True)

    # -------------------------------------------------------------- descoberta
    def _discover(self, folders: list[str]) -> None:
        self.stats.phase = PHASE_DISCOVER
        self._emit_phase(PHASE_DISCOVER, "Procurando fotos nas pastas selecionadas...")
        options = ScanOptions(
            extensions=self.settings.allowed_extensions(),
            recursive=self.settings.recursive,
            follow_symlinks=self.settings.follow_symlinks,
            skip_hidden=self.settings.skip_hidden,
            min_size_bytes=max(0, self.settings.min_file_size_kb) * 1024,
            excluded_names=tuple(self.settings.excluded_folder_names),
        )
        scanner = FolderScanner(options, self._cancel)
        folder_ids = {f["path"]: int(f["id"]) for f in self.repo.list_folders()}

        batch: list[tuple[str, int, int, int | None]] = []
        total_new = total_cached = 0
        for entry in scanner.scan(folders):
            self._wait_if_paused()
            if self._cancel.is_set():
                break
            folder_id = _match_folder(entry.path, folder_ids)
            batch.append((entry.path, entry.size, entry.mtime_ns, folder_id))
            self.stats.files_found += 1
            if len(batch) >= 2000:
                new, cached = self.repo.register_files(batch)
                total_new += new
                total_cached += cached
                batch.clear()
                self._emit_stats()
        if batch:
            new, cached = self.repo.register_files(batch)
            total_new += new
            total_cached += cached

        self.stats.valid_photos = self.stats.files_found
        self.stats.skipped_cached = total_cached
        for path, message in scanner.errors[:50]:
            self._log(f"Aviso: {path} — {message}")
        self._log(
            f"{self.stats.files_found:n} arquivos de imagem encontrados "
            f"({total_new:n} novos ou modificados, {total_cached:n} já analisados anteriormente)."
        )
        self._emit_stats()

    # ------------------------------------------------------------------ análise
    def _analyze(self) -> None:
        pending = self.repo.pending_files()
        self.stats.phase = PHASE_ANALYZE
        if not pending:
            self._log("Nada novo para analisar: usando os resultados já armazenados.")
            self._emit_phase(PHASE_ANALYZE, "Nenhuma foto nova para analisar")
            return

        total = len(pending)
        self._emit_phase(PHASE_ANALYZE, f"Analisando {total:n} fotos...")
        options = AnalyzerOptions(
            analysis_long_side=self.settings.analysis_long_side,
            thumbnail_size=self.settings.thumbnail_size,
            compute_thumbnail=True,
            use_embeddings=self.settings.use_embeddings,
            analyze_bad_photos=self.settings.analyze_bad_photos,
            store_gray_signature=self.settings.store_gray_signature,
            blur_threshold=self.settings.blur_threshold,
            dark_threshold=self.settings.dark_threshold,
            bright_threshold=self.settings.bright_threshold,
            small_photo_max_dim=self.settings.small_photo_max_dim,
            onnx_model_path=self.settings.onnx_model_path,
            use_gpu=self.settings.use_gpu_if_available,
        )
        workers = self.settings.effective_workers()
        self._meter.tick(0)

        if workers <= 1:
            self._analyze_sequential(pending, options)
        else:
            try:
                self._analyze_parallel(pending, options, workers)
            except Exception:  # noqa: BLE001 - se o pool falhar, continuamos em série
                log.exception("Falha no processamento paralelo; voltando ao modo sequencial")
                self._log("Não foi possível usar vários processos; continuando em modo sequencial.")
                remaining = self.repo.pending_files()
                self._analyze_sequential(remaining, options)

    def _analyze_sequential(self, pending: list[tuple[int, str]], options: AnalyzerOptions) -> None:
        batch: list = []
        for file_id, path in pending:
            self._wait_if_paused()
            if self._cancel.is_set():
                break
            batch.append((file_id, analyze_path(path, options)))
            self._account(batch[-1][1])
            if len(batch) >= self.settings.batch_size:
                self.repo.save_analysis_batch(batch)
                batch.clear()
                self._emit_stats()
        if batch:
            self.repo.save_analysis_batch(batch)
        self._emit_stats()

    def _analyze_parallel(self, pending: list[tuple[int, str]], options: AnalyzerOptions, workers: int) -> None:
        in_flight: dict[Future, int] = {}
        queue = deque(pending)
        batch: list = []
        max_in_flight = max(2, workers * 3)

        self._executor = ProcessPoolExecutor(max_workers=workers)
        try:
            while (queue or in_flight) and not self._cancel.is_set():
                self._wait_if_paused()
                while queue and len(in_flight) < max_in_flight and not self._cancel.is_set() and not self.is_paused:
                    file_id, path = queue.popleft()
                    future = self._executor.submit(analyze_path, path, options)
                    in_flight[future] = file_id
                if not in_flight:
                    continue
                done, _pending_futures = wait(list(in_flight), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    file_id = in_flight.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Erro ao analisar arquivo %s", file_id)
                        from .models import AnalysisResult

                        result = AnalysisResult(path="", ok=False, error=f"{type(exc).__name__}: {exc}")
                    batch.append((file_id, result))
                    self._account(result)
                if len(batch) >= self.settings.batch_size:
                    self.repo.save_analysis_batch(batch)
                    batch.clear()
                    self._emit_stats()
            if batch:
                self.repo.save_analysis_batch(batch)
            self._emit_stats()
        finally:
            self._shutdown_executor(cancel=self._cancel.is_set())

    def _shutdown_executor(self, cancel: bool = False) -> None:
        executor, self._executor = self._executor, None
        if executor is None:
            return
        try:
            executor.shutdown(wait=not cancel, cancel_futures=cancel)
        except Exception:  # noqa: BLE001
            log.exception("Falha ao encerrar o pool de processos")

    def _account(self, result) -> None:
        self.stats.analyzed += 1
        if result.ok:
            self.stats.bytes_read += result.size
            if result.bad_flags:
                self.stats.bad_photos += 1
        else:
            self.stats.errors += 1
        self._meter.tick(self.stats.analyzed)
        rate = self._meter.rate()
        self.stats.rate = rate
        remaining = max(0, (self.stats.valid_photos - self.stats.skipped_cached) - self.stats.analyzed)
        self.stats.eta_s = (remaining / rate) if rate > 0.01 else 0.0
        if self.stats.analyzed % 25 == 0:
            self._emit_stats()

    # -------------------------------------------------------------- agrupamento
    def _group(self) -> None:
        self.stats.phase = PHASE_GROUP
        self._emit_phase(PHASE_GROUP, "Comparando as fotos e formando os grupos...")
        signatures = self.repo.load_signatures()
        if len(signatures) < 2:
            self._log("Fotos insuficientes para comparar.")
            return
        self._log(f"Comparando {len(signatures):n} fotos...")

        options = GroupingOptions(
            thresholds=self.settings.thresholds.normalized(),
            strict=self.settings.strict_mode,
            detect_crops=self.settings.detect_crops,
            use_embeddings=self.settings.use_embeddings,
            max_candidates_per_file=self.settings.max_candidates_per_file,
        )

        def progress(done: int, total: int) -> None:
            self._wait_if_paused()
            if total:
                self._emit_phase(PHASE_GROUP, f"Verificando semelhanças: {done:n} de {total:n} comparações")

        groups, gstats = build_groups(signatures, options, self.repo.excluded_pairs(), progress)
        if self._cancel.is_set():
            return
        self.repo.replace_groups(groups)

        summary = self.repo.summary()
        self.stats.groups_total = summary["groups"]
        self.stats.exact_duplicates = summary["exact_duplicates"]
        self.stats.visual_duplicates = summary["visual_duplicates"]
        self.stats.similar_groups = summary["very_similar_groups"] + summary["similar_groups"]
        self.stats.reclaimable_bytes = summary["reclaimable_bytes"]
        self.stats.bad_photos = summary["bad_photos"]
        self._log(
            f"{gstats.candidate_pairs:n} pares candidatos, {gstats.compared_pairs:n} comparações, "
            f"{gstats.groups:n} grupos formados em {gstats.elapsed_s:.1f}s."
        )
        self._emit_stats()

    # ------------------------------------------------------------------ eventos
    def _emit_stats(self) -> None:
        if self.cb.on_stats:
            self.cb.on_stats(self.stats)

    def _emit_phase(self, phase: str, message: str) -> None:
        if self.cb.on_phase:
            self.cb.on_phase(phase, message)

    def _log(self, message: str) -> None:
        log.info(message)
        if self.cb.on_log:
            self.cb.on_log(message)


def _match_folder(path: str, folder_ids: dict[str, int]) -> int | None:
    """Descobre a qual pasta monitorada o arquivo pertence."""
    best: tuple[int, int | None] = (-1, None)
    for folder, fid in folder_ids.items():
        if path.startswith(folder + os.sep) or path == folder:
            if len(folder) > best[0]:
                best = (len(folder), fid)
    return best[1]


def regroup_only(repo: Repository, settings: Settings) -> int:
    """Refaz apenas o agrupamento (após o usuário mudar os limites de similaridade)."""
    signatures = repo.load_signatures()
    options = GroupingOptions(
        thresholds=settings.thresholds.normalized(),
        strict=settings.strict_mode,
        detect_crops=settings.detect_crops,
        use_embeddings=settings.use_embeddings,
        max_candidates_per_file=settings.max_candidates_per_file,
    )
    groups, _stats = build_groups(signatures, options, repo.excluded_pairs())
    repo.replace_groups(groups)
    return len(groups)
