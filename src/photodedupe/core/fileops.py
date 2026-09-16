"""Operações de arquivo - a parte mais sensível do aplicativo.

Princípios que este módulo garante, por construção:

* **Nada acontece sem um plano explícito.** A interface monta um
  :class:`DeletionPlan`, mostra o resumo ao usuário e só então chama
  :meth:`FileManager.execute`.
* **Nada é sobrescrito.** Ao mover para a quarentena, nomes em conflito ganham
  sufixo numérico.
* **Nada é apagado de verdade.** O padrão é mover para a pasta de quarentena
  (dentro do perfil do usuário). A opção alternativa é a Lixeira do sistema.
  A exclusão definitiva nunca é feita pelo aplicativo.
* **Toda operação pode ser desfeita** enquanto os arquivos estiverem na
  quarentena: o histórico guarda origem e destino de cada arquivo.
* **Validação antes de agir**: o arquivo precisa existir, ter o mesmo tamanho
  registrado na análise e a foto "guardada" do grupo precisa existir de fato.
  Qualquer divergência cancela aquele item (e apenas ele).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from ..db.repository import Repository
from ..paths import default_quarantine_dir

log = logging.getLogger(__name__)

try:
    from send2trash import send2trash  # type: ignore

    HAS_SEND2TRASH = True
except Exception:  # noqa: BLE001
    HAS_SEND2TRASH = False

MODE_QUARANTINE = "quarantine"
MODE_TRASH = "trash"

QUARANTINE_INDEX = "_indice_quarentena.json"


@dataclass
class PlanItem:
    """Um arquivo marcado para remoção, com o contexto que justifica a remoção."""

    file_id: int
    path: str
    size: int
    group_id: int = 0
    category: str = ""
    similarity: float = 0.0
    quality: float = 0.0
    keeper_path: str = ""
    keeper_quality: float = 0.0
    problem: str = ""

    @property
    def valid(self) -> bool:
        return not self.problem


@dataclass
class DeletionPlan:
    """Resumo completo do que será feito - exibido antes de qualquer ação."""

    items: list[PlanItem] = field(default_factory=list)
    mode: str = MODE_QUARANTINE
    quarantine_dir: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def valid_items(self) -> list[PlanItem]:
        return [i for i in self.items if i.valid]

    @property
    def blocked_items(self) -> list[PlanItem]:
        return [i for i in self.items if not i.valid]

    @property
    def count(self) -> int:
        return len(self.valid_items)

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.valid_items)

    def summary_text(self) -> str:
        """Texto exibido na confirmação (requisito de segurança)."""
        destino = (
            f"a pasta de quarentena ({self.quarantine_dir})"
            if self.mode == MODE_QUARANTINE
            else "a Lixeira do Windows"
        )
        linhas = [
            f"Você selecionou {self.count:n} arquivo(s) para remoção.",
            f"Espaço que será liberado: {format_bytes(self.total_bytes)}.",
            f"Os arquivos serão enviados para {destino}.",
            "Nenhum arquivo é apagado definitivamente: você pode desfazer esta operação.",
        ]
        if self.blocked_items:
            linhas.append(f"{len(self.blocked_items)} item(ns) foram bloqueados por segurança e não serão tocados.")
        return "\n".join(linhas)

    def to_rows(self) -> list[dict]:
        return [
            {
                "arquivo": i.path,
                "tamanho_bytes": i.size,
                "tamanho": format_bytes(i.size),
                "grupo": i.group_id,
                "categoria": i.category,
                "similaridade": round(i.similarity, 2),
                "qualidade": round(i.quality, 1),
                "foto_mantida": i.keeper_path,
                "qualidade_mantida": round(i.keeper_quality, 1),
                "situacao": "bloqueado: " + i.problem if i.problem else "será removido",
            }
            for i in self.items
        ]


@dataclass
class ExecutionResult:
    batch: str
    moved: int = 0
    failed: int = 0
    freed_bytes: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    destination: str = ""


class FileManager:
    """Executa (e desfaz) as operações de arquivo aprovadas pelo usuário."""

    def __init__(self, repo: Repository, quarantine_dir: str | Path | None = None, keep_structure: bool = True) -> None:
        self.repo = repo
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir else default_quarantine_dir()
        self.keep_structure = keep_structure

    # ------------------------------------------------------------------ plano
    def build_plan(self, mode: str = MODE_QUARANTINE, file_ids: Sequence[int] | None = None) -> DeletionPlan:
        """Monta o plano a partir das escolhas atuais (recomendações + ajustes do usuário)."""
        rows = self.repo.selected_for_removal()
        if file_ids is not None:
            wanted = {int(i) for i in file_ids}
            rows = [r for r in rows if int(r["file_id"]) in wanted]

        keepers: dict[int, tuple[str, float]] = {}
        for group in self.repo.load_groups():
            ref = group.reference
            if ref:
                keepers[group.group_id] = (ref.signature.path, ref.signature.quality)

        plan = DeletionPlan(mode=mode, quarantine_dir=str(self.quarantine_dir))
        for row in rows:
            keeper_path, keeper_quality = keepers.get(int(row["group_id"]), ("", 0.0))
            item = PlanItem(
                file_id=int(row["file_id"]),
                path=row["path"],
                size=int(row["size"] or 0),
                group_id=int(row["group_id"]),
                category=row["category"],
                similarity=float(row["similarity"] or 0.0),
                quality=float(row["quality"] or 0.0),
                keeper_path=keeper_path,
                keeper_quality=keeper_quality,
            )
            item.problem = self._validate(item)
            plan.items.append(item)
        return plan

    def _validate(self, item: PlanItem) -> str:
        """Confere, arquivo por arquivo, se a remoção é segura. Retorna o problema encontrado."""
        path = Path(item.path)
        try:
            if not path.exists():
                return "o arquivo não está mais no disco"
            if not path.is_file():
                return "o caminho não é um arquivo comum"
            real_size = path.stat().st_size
            if item.size and real_size != item.size:
                return "o arquivo foi modificado desde a análise"
        except OSError as exc:
            return f"não foi possível acessar o arquivo ({exc.strerror or exc})"

        if item.keeper_path:
            keeper = Path(item.keeper_path)
            if keeper.resolve() == path.resolve():
                return "é o próprio arquivo recomendado para manter"
            if not keeper.exists():
                return "a foto que seria mantida não está mais no disco"
        else:
            return "não foi possível identificar qual foto seria mantida"

        try:
            if self.quarantine_dir.exists() and self.quarantine_dir.resolve() in path.resolve().parents:
                return "o arquivo já está na quarentena"
        except OSError:
            pass
        return ""

    # -------------------------------------------------------------- execução
    def execute(
        self,
        plan: DeletionPlan,
        confirmed: bool,
        progress: Callable[[int, int], None] | None = None,
    ) -> ExecutionResult:
        """Aplica o plano. **Só funciona com ``confirmed=True``.**"""
        if not confirmed:
            raise PermissionError(
                "A remoção precisa de confirmação explícita do usuário. Nenhum arquivo foi tocado."
            )
        batch = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        result = ExecutionResult(batch=batch, destination=str(self.quarantine_dir) if plan.mode == MODE_QUARANTINE else "Lixeira do sistema")
        items = plan.valid_items
        journal: list[tuple[str, int | None, str, str, int, str, str]] = []
        moved_ids: list[int] = []

        for index, item in enumerate(items, start=1):
            problem = self._validate(item)   # revalida imediatamente antes de mover
            if problem:
                result.failed += 1
                result.errors.append((item.path, problem))
                journal.append(("bloqueado", item.file_id, item.path, "", item.size, "erro", problem))
                continue
            try:
                if plan.mode == MODE_TRASH:
                    destination = self._to_trash(item.path)
                    action = "lixeira"
                else:
                    destination = self._to_quarantine(item.path)
                    action = "quarentena"
                result.moved += 1
                result.freed_bytes += item.size
                moved_ids.append(item.file_id)
                journal.append((action, item.file_id, item.path, destination, item.size, "ok", ""))
            except Exception as exc:  # noqa: BLE001 - um erro não pode interromper o lote
                log.exception("Falha ao mover %s", item.path)
                result.failed += 1
                result.errors.append((item.path, str(exc)))
                journal.append(("falha", item.file_id, item.path, "", item.size, "erro", str(exc)))
            if progress is not None:
                progress(index, len(items))

        if journal:
            self.repo.record_actions(batch, journal)
        if moved_ids:
            self.repo.mark_removed(moved_ids)
        if plan.mode == MODE_QUARANTINE and result.moved:
            self._write_quarantine_index(batch, journal)
        return result

    def _to_quarantine(self, src: str) -> str:
        source = Path(src)
        if self.keep_structure:
            drive, tail = os.path.splitdrive(str(source.parent))
            drive_name = drive.replace(":", "").replace("\\", "").replace("/", "") or "raiz"
            relative = Path(drive_name) / Path(tail.lstrip("\\/"))
            target_dir = self.quarantine_dir / relative
        else:
            target_dir = self.quarantine_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir / source.name)
        shutil.move(str(source), str(target))
        return str(target)

    def _to_trash(self, src: str) -> str:
        if not HAS_SEND2TRASH:
            raise RuntimeError(
                "O envio para a Lixeira exige o pacote 'send2trash'. "
                "Use o modo quarentena ou instale a dependência."
            )
        send2trash(str(Path(src)))
        return "Lixeira do sistema"

    def _write_quarantine_index(self, batch: str, journal: list) -> None:
        """Índice legível dentro da quarentena, para restauração manual se preciso."""
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            index_path = self.quarantine_dir / QUARANTINE_INDEX
            data = []
            if index_path.exists():
                try:
                    data = json.loads(index_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = []
            data.append(
                {
                    "lote": batch,
                    "data": datetime.now().isoformat(timespec="seconds"),
                    "arquivos": [
                        {"origem": row[2], "destino": row[3], "tamanho": row[4]}
                        for row in journal
                        if row[5] == "ok"
                    ],
                }
            )
            index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001 - o índice é um apoio, não pode quebrar a operação
            log.exception("Não foi possível gravar o índice da quarentena")

    # ------------------------------------------------------------------ desfazer
    def can_undo(self, batch: str) -> bool:
        actions = self.repo.batch_actions(batch)
        return any(a["action"] == "quarentena" and a["status"] == "ok" for a in actions)

    def undo(self, batch: str, progress: Callable[[int, int], None] | None = None) -> ExecutionResult:
        """Devolve os arquivos da quarentena para os locais originais."""
        actions = [a for a in self.repo.batch_actions(batch) if a["status"] == "ok"]
        result = ExecutionResult(batch=batch)
        restored_ids: list[int] = []
        for index, action in enumerate(actions, start=1):
            if action["action"] != "quarentena":
                result.failed += 1
                result.errors.append((action["src"], "envios para a Lixeira devem ser restaurados pelo Windows"))
                continue
            src = Path(action["dst"])
            target = Path(action["src"])
            try:
                if not src.exists():
                    raise FileNotFoundError("arquivo não encontrado na quarentena")
                target.parent.mkdir(parents=True, exist_ok=True)
                final = unique_path(target)
                shutil.move(str(src), str(final))
                result.moved += 1
                result.freed_bytes += int(action["size"] or 0)
                self.repo.mark_action_undone(int(action["id"]))
                if action["file_id"]:
                    restored_ids.append(int(action["file_id"]))
            except Exception as exc:  # noqa: BLE001
                log.exception("Falha ao restaurar %s", src)
                result.failed += 1
                result.errors.append((str(src), str(exc)))
            if progress is not None:
                progress(index, len(actions))
        if restored_ids:
            self.repo.restore_status(restored_ids)
        return result

    # ------------------------------------------------------------- exportação
    def export_plan(self, plan: DeletionPlan, path: str | Path) -> Path:
        """Exporta o plano antes de executar (requisito de segurança)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = plan.to_rows()
        if target.suffix.lower() == ".json":
            payload = {
                "gerado_em": plan.created_at,
                "modo": plan.mode,
                "quarentena": plan.quarantine_dir,
                "total_arquivos": plan.count,
                "espaco_liberado_bytes": plan.total_bytes,
                "itens": rows,
            }
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            with open(target, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["arquivo"], delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
        return target

    def quarantine_size(self) -> tuple[int, int]:
        """(quantidade de arquivos, bytes) atualmente na quarentena."""
        count = total = 0
        if not self.quarantine_dir.exists():
            return (0, 0)
        for root, _dirs, files in os.walk(self.quarantine_dir):
            for name in files:
                if name == QUARANTINE_INDEX:
                    continue
                try:
                    total += (Path(root) / name).stat().st_size
                    count += 1
                except OSError:
                    continue
        return count, total

    def empty_quarantine(self, confirmed: bool) -> int:
        """Apaga definitivamente o conteúdo da quarentena (ação explícita do usuário)."""
        if not confirmed:
            raise PermissionError("Esvaziar a quarentena exige confirmação explícita.")
        removed = 0
        if not self.quarantine_dir.exists():
            return 0
        for child in self.quarantine_dir.iterdir():
            if child.name == QUARANTINE_INDEX:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                log.exception("Falha ao esvaziar a quarentena em %s", child)
        return removed


def unique_path(target: Path) -> Path:
    """Devolve um caminho livre, sem nunca sobrescrever um arquivo existente."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(1, 10_000):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem} ({uuid.uuid4().hex[:8]}){suffix}")


def format_bytes(value: float) -> str:
    """Formata bytes no padrão brasileiro (vírgula decimal)."""
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f}".replace(".", ",") + f" {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"
