#!/usr/bin/env python3
"""Mede desempenho e precisão do PhotoDedupe em uma biblioteca sintética.

Executa o pipeline completo sobre a pasta gerada por ``gerar_biblioteca.py`` e
compara o resultado com o gabarito, reportando:

* tempo por fase, velocidade (fotos/s) e pico de memória;
* recall por tipo de duplicata (exata, visual);
* falsos positivos (fotos de cenas diferentes no mesmo grupo de duplicatas);
* como foram tratadas as fotos em sequência (rajadas) e os recortes.

Uso::

    python tools/benchmark.py --biblioteca /caminho/biblioteca [--processos 4]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DUP_TIERS = {"exact", "visual"}
DUPLICATE_RELATIONS = {"original", "exact", "visual"}


def peak_memory_mb() -> float:
    """Pico de memória do processo e dos filhos, em MB."""
    unit = 1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / unit
    return max(own, children)


def run(args) -> dict:
    from photodedupe.config import Settings
    from photodedupe.core.pipeline import AnalysisPipeline, PipelineCallbacks
    from photodedupe.db.database import Database
    from photodedupe.db.repository import Repository

    library = Path(args.biblioteca)
    truth_file = library / "verdade.json"
    truth = {r["path"]: r for r in json.loads(truth_file.read_text(encoding="utf-8"))}

    settings = Settings.load()
    settings.min_file_size_kb = 1
    settings.analyze_bad_photos = True
    if args.processos:
        settings.workers = args.processos
    if args.limiar:
        settings.thresholds.duplicate_min = args.limiar
        settings.thresholds = settings.thresholds.normalized()

    db = Database()
    repo = Repository(db)
    repo.add_folder(str(library))

    phases: dict[str, float] = {}
    marks = {"last": time.perf_counter()}

    def on_phase(phase: str, _message: str) -> None:
        now = time.perf_counter()
        phases[phase] = phases.get(phase, 0.0)
        marks["last"] = now

    started = time.perf_counter()
    pipeline = AnalysisPipeline(repo, settings, PipelineCallbacks(on_phase=on_phase, on_log=lambda m: print("  " + m)))
    stats = pipeline.run([str(library)])
    elapsed = time.perf_counter() - started

    groups = repo.load_groups(limit=1_000_000, order="id")
    summary = repo.summary()
    result = evaluate(groups, truth)
    result.update(safety_check(groups, truth))
    result.update(
        {
            "arquivos": stats.files_found,
            "analisadas": stats.analyzed,
            "erros": stats.errors,
            "tempo_total_s": round(elapsed, 2),
            "fotos_por_segundo": round(stats.analyzed / elapsed, 1) if elapsed else 0,
            "memoria_pico_mb": round(peak_memory_mb(), 1),
            "banco_mb": round(db.size_bytes() / 1e6, 1),
            "resumo": summary,
        }
    )
    db.close()
    return result


def evaluate(groups, truth: dict) -> dict:
    """Compara os grupos encontrados com o gabarito."""
    by_origin_group: dict[int, set[int]] = defaultdict(set)   # origem -> grupos (níveis de duplicata)
    group_of_file: dict[str, list[tuple[int, str]]] = defaultdict(list)
    false_positive_pairs: list[tuple[str, str, str, float]] = []
    tier_by_relation: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for group in groups:
        tier = group.category.value
        paths = [m.signature.path for m in group.members]
        origins = []
        for member in group.members:
            info = truth.get(member.signature.path)
            relation = info["relation"] if info else "desconhecido"
            origin = info["origin"] if info else -1
            origins.append((origin, relation, member.signature.path, member.similarity))
            group_of_file[member.signature.path].append((group.group_id, tier))
            tier_by_relation[relation][tier] += 1
            if tier in DUP_TIERS and relation in DUPLICATE_RELATIONS:
                by_origin_group[origin].add(group.group_id)

        if tier in DUP_TIERS:
            reference_origin = origins[0][0]
            for origin, _relation, path, similarity in origins[1:]:
                if origin != reference_origin:
                    false_positive_pairs.append((paths[0], path, tier, similarity))

    # Recall: cada variante precisa estar em um grupo de duplicata com outro
    # arquivo da mesma cena.
    expected = defaultdict(list)
    for path, info in truth.items():
        expected[info["origin"]].append((path, info["relation"]))

    detected = defaultdict(int)
    missed: dict[str, list[str]] = defaultdict(list)
    totals = defaultdict(int)
    for _origin, items in expected.items():
        family = {path for path, relation in items if relation in DUPLICATE_RELATIONS}
        for path, relation in items:
            if relation not in ("exact", "visual"):
                continue
            totals[relation] += 1
            my_groups = {gid for gid, tier in group_of_file.get(path, []) if tier in DUP_TIERS}
            if not my_groups:
                missed[relation].append(path)
                continue
            shares = False
            for other in family:
                if other == path:
                    continue
                other_groups = {gid for gid, tier in group_of_file.get(other, []) if tier in DUP_TIERS}
                if my_groups & other_groups:
                    shares = True
                    break
            if shares:
                detected[relation] += 1
            else:
                missed[relation].append(path)

    burst_in_dup = sum(count for tier, count in tier_by_relation["burst"].items() if tier in DUP_TIERS)
    burst_total = sum(1 for info in truth.values() if info["relation"] == "burst")
    crop_total = sum(1 for info in truth.values() if info["relation"] == "crop")

    return {
        "grupos": len(groups),
        "recall_exatas": _ratio(detected["exact"], totals["exact"]),
        "recall_visuais": _ratio(detected["visual"], totals["visual"]),
        "exatas_detectadas": f"{detected['exact']}/{totals['exact']}",
        "visuais_detectadas": f"{detected['visual']}/{totals['visual']}",
        "falsos_positivos": len(false_positive_pairs),
        "exemplos_falsos_positivos": [
            {"a": Path(a).name, "b": Path(b).name, "nivel": tier, "similaridade": round(sim, 2)}
            for a, b, tier, sim in false_positive_pairs[:10]
        ],
        "rajadas_em_grupos_de_duplicata": f"{burst_in_dup}/{burst_total}",
        "distribuicao_por_relacao": {
            relation: dict(tiers) for relation, tiers in sorted(tier_by_relation.items())
        },
        "recortes_total": crop_total,
        "nao_detectadas": {relation: [Path(p).name for p in paths[:8]] for relation, paths in missed.items()},
    }


def safety_check(groups, truth: dict) -> dict:
    """Verificação de segurança: aplicar TODAS as recomendações perderia alguma foto?

    Simula a remoção de tudo o que o aplicativo recomenda remover e confere se
    cada cena continua tendo pelo menos um arquivo - e se alguma foto sem
    duplicata alguma foi marcada.
    """
    marcados: set[str] = set()
    for group in groups:
        for member in group.members:
            if member.effective_choice == "remove":
                marcados.add(member.signature.path)

    por_origem: dict[int, list[str]] = defaultdict(list)
    for path, info in truth.items():
        por_origem[info["origin"]].append(path)

    cenas_perdidas = []
    unicas_marcadas = []
    rajadas_marcadas = []
    for origin, paths in por_origem.items():
        sobreviventes = [p for p in paths if p not in marcados]
        if not sobreviventes:
            cenas_perdidas.append(origin)
        for path in paths:
            if path not in marcados:
                continue
            relacao = truth[path]["relation"]
            if relacao == "burst":
                rajadas_marcadas.append(path)
            elif relacao == "original" and not any(
                truth[p]["relation"] in ("exact", "visual") and p not in marcados for p in paths
            ):
                unicas_marcadas.append(path)

    return {
        "cenas_que_perderiam_todas_as_fotos": len(cenas_perdidas),
        "fotos_marcadas_para_remocao": len(marcados),
        "rajadas_marcadas_para_remocao": len(rajadas_marcadas),
        "originais_marcadas_sem_copia_sobrevivente": len(unicas_marcadas),
        "exemplos_rajadas_marcadas": [Path(p).name for p in rajadas_marcadas[:6]],
    }


def _ratio(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark de desempenho e precisão do PhotoDedupe")
    parser.add_argument("--biblioteca", required=True)
    parser.add_argument("--processos", type=int, default=0)
    parser.add_argument("--limiar", type=float, default=0.0)
    parser.add_argument("--saida-json", default="")
    parser.add_argument("--manter-banco", action="store_true", help="não usa um banco temporário")
    args = parser.parse_args()

    if not args.manter_banco:
        tmp = tempfile.mkdtemp(prefix="photodedupe-bench-")
        os.environ["PHOTODEDUPE_HOME"] = tmp
        print(f"Banco temporário: {tmp}")

    result = run(args)
    print("\n" + "=" * 74)
    print("RESULTADO DO BENCHMARK")
    print("=" * 74)
    for key in (
        "arquivos", "analisadas", "erros", "tempo_total_s", "fotos_por_segundo",
        "memoria_pico_mb", "banco_mb", "grupos",
    ):
        print(f"{key:32s}: {result[key]}")
    print("-" * 74)
    print(f"{'recall duplicatas exatas':32s}: {result['recall_exatas']}%  ({result['exatas_detectadas']})")
    print(f"{'recall duplicatas visuais':32s}: {result['recall_visuais']}%  ({result['visuais_detectadas']})")
    print(f"{'falsos positivos (cenas distintas)':32s}: {result['falsos_positivos']}")
    print(f"{'rajadas tratadas como duplicata':32s}: {result['rajadas_em_grupos_de_duplicata']}")
    print("-" * 74)
    print("SEGURANÇA (simulando a aplicação de todas as recomendações)")
    print(f"{'fotos marcadas para remoção':32s}: {result['fotos_marcadas_para_remocao']}")
    print(f"{'cenas que perderiam TODAS as fotos':32s}: {result['cenas_que_perderiam_todas_as_fotos']}")
    print(f"{'rajadas marcadas para remoção':32s}: {result['rajadas_marcadas_para_remocao']}")
    print(f"{'originais sem cópia sobrevivente':32s}: {result['originais_marcadas_sem_copia_sobrevivente']}")
    print("-" * 74)
    print("Distribuição por relação (relação -> nível do grupo):")
    for relation, tiers in result["distribuicao_por_relacao"].items():
        print(f"  {relation:10s}: {tiers}")
    if result["exemplos_falsos_positivos"]:
        print("\nExemplos de falsos positivos:")
        for item in result["exemplos_falsos_positivos"]:
            print(f"  {item['a']} ~ {item['b']} ({item['nivel']}, {item['similaridade']}%)")
    if any(result["nao_detectadas"].values()):
        print("\nNão detectadas:")
        for relation, paths in result["nao_detectadas"].items():
            if paths:
                print(f"  {relation}: {', '.join(paths)}")

    if args.saida_json:
        Path(args.saida_json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nResultado gravado em {args.saida_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
