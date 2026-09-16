"""Interface de linha de comando (uso avançado, testes e automação).

A interface gráfica é a forma normal de usar o aplicativo; esta CLI existe para
quem quiser rodar a análise em lote ou conferir resultados sem abrir a janela.
Assim como na interface, **nenhum arquivo é removido sem confirmação explícita**
(aqui, o parâmetro ``--confirmar``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_DISPLAY_NAME, __version__
from .config import Settings
from .core.fileops import MODE_QUARANTINE, MODE_TRASH, FileManager, format_bytes
from .core.pipeline import AnalysisPipeline, PipelineCallbacks
from .core.reports import collect, export
from .db import Database, Repository
from .logging_setup import setup_locale, setup_logging


def _repo() -> tuple[Database, Repository]:
    db = Database()
    return db, Repository(db)


def cmd_scan(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.limiar_duplicata:
        settings.thresholds.duplicate_min = args.limiar_duplicata
    if args.processos:
        settings.workers = args.processos
    settings.thresholds = settings.thresholds.normalized()

    db, repo = _repo()
    try:
        folders = [str(Path(f).resolve()) for f in args.pastas]
        for folder in folders:
            repo.add_folder(folder)

        def on_stats(stats):
            sys.stdout.write(
                f"\r{stats.phase:12s} {stats.analyzed:>7d}/{stats.valid_photos:<7d} "
                f"({stats.percent:5.1f}%) {stats.rate:6.1f} fotos/s"
            )
            sys.stdout.flush()

        pipeline = AnalysisPipeline(
            repo, settings,
            PipelineCallbacks(on_stats=on_stats, on_log=lambda m: print("\n" + m)),
        )
        stats = pipeline.run(folders)
        print()
        summary = repo.summary()
        print(f"Fotos analisadas............: {summary['photos']}")
        print(f"Duplicatas exatas...........: {summary['exact_duplicates']} em {summary['exact_groups']} grupos")
        print(f"Duplicatas visuais..........: {summary['visual_duplicates']} em {summary['visual_groups']} grupos")
        print(f"Grupos muito semelhantes....: {summary['very_similar_groups']}")
        print(f"Grupos semelhantes..........: {summary['similar_groups']}")
        print(f"Espaço liberável............: {format_bytes(summary['reclaimable_bytes'])}")
        print(f"Fotos com problema..........: {summary['bad_photos']}")
        return 0 if stats.phase == "concluido" else 1
    finally:
        db.close()


def cmd_report(args: argparse.Namespace) -> int:
    db, repo = _repo()
    try:
        data = collect(repo, Settings.load().to_dict())
        target = export(data, args.saida)
        print(f"Relatório gravado em {target}")
        return 0
    finally:
        db.close()


def cmd_plan(args: argparse.Namespace) -> int:
    settings = Settings.load()
    db, repo = _repo()
    try:
        manager = FileManager(repo, settings.quarantine_path(), settings.keep_folder_structure_in_quarantine)
        plan = manager.build_plan(MODE_TRASH if args.lixeira else MODE_QUARANTINE)
        print(plan.summary_text())
        if args.exportar:
            print("Plano exportado para", manager.export_plan(plan, args.exportar))
        if args.confirmar:
            result = manager.execute(plan, confirmed=True)
            print(f"Movidos: {result.moved} | Falhas: {result.failed} | Liberado: {format_bytes(result.freed_bytes)}")
            print(f"Lote: {result.batch} (use 'photodedupe-cli desfazer {result.batch}' para reverter)")
        else:
            print("\nNenhum arquivo foi tocado. Use --confirmar para executar o plano.")
        return 0
    finally:
        db.close()


def cmd_undo(args: argparse.Namespace) -> int:
    settings = Settings.load()
    db, repo = _repo()
    try:
        manager = FileManager(repo, settings.quarantine_path())
        result = manager.undo(args.lote)
        print(f"Restaurados: {result.moved} | Falhas: {result.failed}")
        for path, error in result.errors:
            print(f"  ! {path}: {error}")
        return 0
    finally:
        db.close()


def cmd_status(args: argparse.Namespace) -> int:
    db, repo = _repo()
    try:
        summary = repo.summary()
        for key, value in summary.items():
            printable = format_bytes(value) if key.endswith("bytes") else value
            print(f"{key:24s}: {printable}")
        print(f"{'banco':24s}: {db.path} ({format_bytes(db.size_bytes())})")
        for batch in repo.list_batches(10):
            print(f"lote {batch['batch']}: {batch['n']} arquivos, {format_bytes(batch['bytes'] or 0)}")
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photodedupe-cli", description=f"{APP_DISPLAY_NAME} - linha de comando"
    )
    parser.add_argument("--versao", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="comando", required=True)

    scan = sub.add_parser("analisar", help="analisa uma ou mais pastas")
    scan.add_argument("pastas", nargs="+", help="pastas a analisar")
    scan.add_argument("--processos", type=int, default=0, help="número de processos (0 = automático)")
    scan.add_argument("--limiar-duplicata", type=float, default=0.0, help="similaridade mínima para duplicata (%%)")
    scan.set_defaults(func=cmd_scan)

    report = sub.add_parser("relatorio", help="exporta o relatório da última análise")
    report.add_argument("saida", help="arquivo de saída (.csv, .json, .xlsx ou .pdf)")
    report.set_defaults(func=cmd_report)

    plan = sub.add_parser("plano", help="mostra (e opcionalmente executa) o plano de remoção")
    plan.add_argument("--confirmar", action="store_true", help="executa o plano de fato")
    plan.add_argument("--lixeira", action="store_true", help="usa a Lixeira em vez da quarentena")
    plan.add_argument("--exportar", help="grava o plano em CSV/JSON antes de executar")
    plan.set_defaults(func=cmd_plan)

    undo = sub.add_parser("desfazer", help="desfaz um lote de remoções")
    undo.add_argument("lote", help="identificador do lote")
    undo.set_defaults(func=cmd_undo)

    status = sub.add_parser("status", help="mostra o resumo do banco local")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging(to_console=False)
    setup_locale()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
