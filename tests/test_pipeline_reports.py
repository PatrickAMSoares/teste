"""Testes de ponta a ponta: varredura, pipeline, relatórios e configuração."""

from __future__ import annotations

import json
import threading

from conftest import make_photo

from photodedupe.config import RASTER_EXTENSIONS, Settings, Thresholds
from photodedupe.core import reports
from photodedupe.core.models import Category
from photodedupe.core.pipeline import AnalysisPipeline, PipelineCallbacks, regroup_only
from photodedupe.core.scanner import FolderScanner, ScanOptions


# ------------------------------------------------------------------ varredura
def test_varredura_filtra_por_extensao_e_tamanho(tmp_path):
    (tmp_path / "sub").mkdir()
    make_photo(1, 400, 300).save(tmp_path / "foto.jpg", quality=90)
    make_photo(2, 400, 300).save(tmp_path / "sub" / "outra.png")
    (tmp_path / "documento.txt").write_text("não é foto")
    (tmp_path / "minuscula.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    opcoes = ScanOptions(extensions=set(RASTER_EXTENSIONS), min_size_bytes=1024)
    encontrados = [e.path for e in FolderScanner(opcoes).scan([str(tmp_path)])]
    assert any(p.endswith("foto.jpg") for p in encontrados)
    assert any(p.endswith("outra.png") for p in encontrados)
    assert not any(p.endswith(".txt") for p in encontrados)
    assert not any("minuscula" in p for p in encontrados)


def test_varredura_ignora_pastas_excluidas_e_nao_recursiva(tmp_path):
    (tmp_path / "node_modules").mkdir()
    make_photo(3, 400, 300).save(tmp_path / "node_modules" / "icone.png")
    (tmp_path / "album").mkdir()
    make_photo(4, 400, 300).save(tmp_path / "album" / "foto.jpg", quality=90)
    make_photo(5, 400, 300).save(tmp_path / "raiz.jpg", quality=90)

    opcoes = ScanOptions(
        extensions=set(RASTER_EXTENSIONS), min_size_bytes=0, excluded_names=("node_modules",)
    )
    caminhos = [e.path for e in FolderScanner(opcoes).scan([str(tmp_path)])]
    assert not any("node_modules" in p for p in caminhos)
    assert len(caminhos) == 2

    somente_raiz = ScanOptions(extensions=set(RASTER_EXTENSIONS), min_size_bytes=0, recursive=False)
    caminhos = [e.path for e in FolderScanner(somente_raiz).scan([str(tmp_path)])]
    assert [p.split("/")[-1] for p in caminhos] == ["raiz.jpg"]


def test_varredura_pode_ser_cancelada(tmp_path):
    for i in range(20):
        make_photo(i, 200, 150).save(tmp_path / f"f{i}.jpg", quality=80)
    cancelar = threading.Event()
    cancelar.set()
    opcoes = ScanOptions(extensions=set(RASTER_EXTENSIONS), min_size_bytes=0)
    assert list(FolderScanner(opcoes, cancelar).scan([str(tmp_path)])) == []


# ------------------------------------------------------------------- pipeline
def test_pipeline_completo(library, repo, settings):
    eventos: list[str] = []
    pipeline = AnalysisPipeline(
        repo, settings, PipelineCallbacks(on_log=eventos.append, on_phase=lambda f, m: eventos.append(f))
    )
    stats = pipeline.run([str(library)])

    assert stats.phase == "concluido"
    assert stats.files_found == 6
    assert stats.analyzed == 6
    assert stats.errors == 0
    assert "descoberta" in eventos and "agrupamento" in eventos

    resumo = repo.summary()
    assert resumo["photos"] == 6
    assert resumo["exact_groups"] == 1          # a cópia byte a byte
    assert resumo["visual_groups"] >= 1         # recompressão/redimensionamento
    assert resumo["reclaimable_bytes"] > 0

    # A foto diferente e a foto em sequência não podem estar em grupos de duplicata.
    duplicadas = set()
    for grupo in repo.load_groups(categories=[Category.EXACT.value, Category.VISUAL.value]):
        duplicadas.update(m.signature.name for m in grupo.members)
    assert "IMG_0002.jpg" not in duplicadas
    assert "IMG_0001_seq.jpg" not in duplicadas


def test_segunda_analise_reaproveita_o_cache(library, repo, settings):
    AnalysisPipeline(repo, settings).run([str(library)])
    primeira = repo.summary()

    pipeline = AnalysisPipeline(repo, settings)
    stats = pipeline.run([str(library)])
    assert stats.analyzed == 0                  # nada foi reanalisado
    assert stats.skipped_cached == 6
    assert repo.summary()["photos"] == primeira["photos"]


def test_cancelamento_interrompe_sem_perder_o_que_ja_foi_feito(library, repo, settings):
    pipeline = AnalysisPipeline(repo, settings)
    pipeline.cancel()
    stats = pipeline.run([str(library)])
    assert stats.phase == "cancelado"
    assert repo.summary()["groups"] == 0


def test_reagrupar_com_limite_mais_rigido(library, repo, settings):
    AnalysisPipeline(repo, settings).run([str(library)])
    antes = repo.summary()["visual_groups"] + repo.summary()["exact_groups"]
    assert antes >= 1

    settings.thresholds = Thresholds(duplicate_min=99.99, very_similar_min=99.5, similar_min=99.0)
    regroup_only(repo, settings)
    depois = repo.summary()
    # Com um limite quase absoluto, só a cópia byte a byte continua sendo duplicata.
    assert depois["exact_groups"] == 1
    assert depois["visual_groups"] == 0


# ----------------------------------------------------------------- relatórios
def test_exporta_relatorio_nos_quatro_formatos(library, repo, settings, tmp_path):
    AnalysisPipeline(repo, settings).run([str(library)])
    dados = reports.collect(repo, settings.to_dict())

    assert dados.summary["photos"] == 6
    assert dados.group_rows()
    linhas = dict(dados.summary_lines())
    assert "Fotos analisadas" in linhas

    for formato in ("csv", "json", "xlsx", "pdf"):
        destino = reports.export(dados, tmp_path / f"relatorio.{formato}")
        assert destino.exists() and destino.stat().st_size > 400

    conteudo = json.loads((tmp_path / "relatorio.json").read_text(encoding="utf-8"))
    assert conteudo["resumo"]["photos"] == 6
    assert conteudo["grupos"][0]["fotos"][0]["recomendacao"] in ("keep", "remove")


# --------------------------------------------------------------- configuração
def test_configuracao_salva_e_recarrega(app_home):
    cfg = Settings()
    cfg.folders = ["/fotos/2024"]
    cfg.thresholds.duplicate_min = 97.5
    cfg.workers = 3
    cfg.save()

    recarregada = Settings.load()
    assert recarregada.folders == ["/fotos/2024"]
    assert recarregada.thresholds.duplicate_min == 97.5
    assert recarregada.workers == 3
    assert recarregada.allow_external_services is False      # privacidade por padrão


def test_limites_sao_normalizados():
    limites = Thresholds(duplicate_min=90.0, very_similar_min=95.0, similar_min=99.0).normalized()
    assert limites.duplicate_min > limites.very_similar_min > limites.similar_min


def test_extensoes_respeitam_a_opcao_de_raw():
    cfg = Settings()
    cfg.include_raw = False
    assert ".cr2" not in cfg.allowed_extensions()
    cfg.include_raw = True
    assert ".cr2" in cfg.allowed_extensions()
