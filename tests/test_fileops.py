"""Testes das operações de arquivo - a parte mais sensível do aplicativo."""

from __future__ import annotations

import shutil

import pytest
from conftest import make_photo

from photodedupe.core.analyzer import analyze_path
from photodedupe.core.fileops import (
    MODE_QUARANTINE,
    FileManager,
    format_bytes,
    unique_path,
)
from photodedupe.core.grouping import build_groups


@pytest.fixture()
def preparado(tmp_path, repo):
    """Banco com um grupo de duplicatas exatas pronto para remoção."""
    fotos = tmp_path / "fotos"
    fotos.mkdir()
    original = fotos / "IMG_0001.jpg"
    make_photo(3, 900, 700).save(original, quality=95)
    copia = fotos / "IMG_0001 (1).jpg"
    shutil.copy2(original, copia)

    entradas = [(str(p), p.stat().st_size, p.stat().st_mtime_ns, None) for p in (original, copia)]
    repo.register_files(entradas)
    repo.save_analysis_batch([(fid, analyze_path(path)) for fid, path in repo.pending_files()])
    grupos, _ = build_groups(repo.load_signatures())
    repo.replace_groups(grupos)

    manager = FileManager(repo, tmp_path / "quarentena")
    return manager, original, copia


def test_plano_descreve_a_operacao(preparado):
    manager, original, copia = preparado
    plano = manager.build_plan(MODE_QUARANTINE)

    assert plano.count == 1
    assert plano.total_bytes == copia.stat().st_size
    texto = plano.summary_text()
    assert "1 arquivo(s) para remoção" in texto
    assert "quarentena" in texto
    assert "desfazer" in texto
    item = plano.valid_items[0]
    assert item.keeper_path == str(original)


def test_execucao_exige_confirmacao_explicita(preparado):
    manager, original, copia = preparado
    plano = manager.build_plan()
    with pytest.raises(PermissionError):
        manager.execute(plano, confirmed=False)
    assert copia.exists()      # nada foi tocado


def test_move_para_quarentena_e_desfaz(preparado):
    manager, original, copia = preparado
    plano = manager.build_plan()
    resultado = manager.execute(plano, confirmed=True)

    assert resultado.moved == 1
    assert resultado.failed == 0
    assert not copia.exists()
    assert original.exists()              # a foto recomendada continua no lugar
    assert manager.quarantine_size()[0] == 1

    desfeito = manager.undo(resultado.batch)
    assert desfeito.moved == 1
    assert copia.exists()
    assert manager.quarantine_size()[0] == 0


def test_arquivo_modificado_apos_a_analise_e_bloqueado(preparado):
    manager, original, copia = preparado
    plano = manager.build_plan()
    copia.write_bytes(copia.read_bytes() + b"conteudo extra")   # muda o arquivo

    assert plano.valid_items                # o plano foi montado antes da mudança
    resultado = manager.execute(plano, confirmed=True)
    assert resultado.moved == 0
    assert resultado.failed == 1
    assert copia.exists()
    assert "modificado" in resultado.errors[0][1]


def test_nunca_sobrescreve_arquivo_existente(tmp_path):
    alvo = tmp_path / "foto.jpg"
    alvo.write_bytes(b"original")
    primeiro = unique_path(alvo)
    assert primeiro.name == "foto (1).jpg"
    primeiro.write_bytes(b"outro")
    assert unique_path(alvo).name == "foto (2).jpg"


def test_quarentena_preserva_estrutura_de_pastas(tmp_path, repo):
    origem = tmp_path / "album" / "2024"
    origem.mkdir(parents=True)
    arquivo = origem / "foto.jpg"
    make_photo(9, 400, 300).save(arquivo, quality=90)
    repo.register_files([(str(arquivo), arquivo.stat().st_size, arquivo.stat().st_mtime_ns, None)])
    repo.save_analysis_batch([(fid, analyze_path(p)) for fid, p in repo.pending_files()])

    manager = FileManager(repo, tmp_path / "quarentena", keep_structure=True)
    file_id = repo.file_id_for_path(str(arquivo))
    plano = manager.build_manual_plan([file_id])
    assert plano.count == 1

    resultado = manager.execute(plano, confirmed=True)
    assert resultado.moved == 1
    movidos = list((tmp_path / "quarentena").rglob("foto.jpg"))
    assert len(movidos) == 1
    assert "2024" in str(movidos[0])
    # o índice legível fica na raiz da quarentena
    assert (tmp_path / "quarentena" / "_indice_quarentena.json").exists()


def test_exportacao_do_plano(preparado, tmp_path):
    manager, _original, _copia = preparado
    plano = manager.build_plan()

    csv_path = manager.export_plan(plano, tmp_path / "plano.csv")
    conteudo = csv_path.read_text(encoding="utf-8-sig")
    assert "arquivo" in conteudo and "foto_mantida" in conteudo

    json_path = manager.export_plan(plano, tmp_path / "plano.json")
    import json

    dados = json.loads(json_path.read_text(encoding="utf-8"))
    assert dados["total_arquivos"] == 1
    assert dados["itens"][0]["situacao"] == "será removido"


def test_esvaziar_quarentena_exige_confirmacao(preparado):
    manager, _original, _copia = preparado
    manager.execute(manager.build_plan(), confirmed=True)
    with pytest.raises(PermissionError):
        manager.empty_quarantine(confirmed=False)
    assert manager.quarantine_size()[0] == 1

    manager.empty_quarantine(confirmed=True)
    assert manager.quarantine_size()[0] == 0


def test_foto_principal_nunca_entra_no_plano(preparado):
    manager, original, _copia = preparado
    plano = manager.build_plan()
    assert all(item.path != str(original) for item in plano.items)


def test_formatacao_de_bytes_em_portugues():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1,50 KB"
    assert format_bytes(5 * 1024 ** 3).endswith("GB")
    assert "," in format_bytes(8.43 * 1024 ** 3)
