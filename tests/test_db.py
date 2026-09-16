"""Testes do banco local: esquema, cache incremental, grupos e migração."""

from __future__ import annotations

import sqlite3

from conftest import make_photo

from photodedupe.core.analyzer import analyze_path
from photodedupe.core.grouping import build_groups
from photodedupe.core.models import Category
from photodedupe.db.database import SCHEMA_VERSION, Database


def _registrar(repo, caminhos, folder_id=None):
    entradas = [(str(p), p.stat().st_size, p.stat().st_mtime_ns, folder_id) for p in caminhos]
    return repo.register_files(entradas)


def test_esquema_criado_com_versao(db):
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert set(db.counts()) >= {"files", "photos", "groups"}


def test_registro_incremental_ignora_arquivos_inalterados(tmp_path, repo):
    a = tmp_path / "a.jpg"
    make_photo(1, 400, 300).save(a, quality=90)

    novos, cache = _registrar(repo, [a])
    assert (novos, cache) == (1, 0)

    # Enquanto não foi analisado, o arquivo continua na fila (não conta como cache).
    novos, cache = _registrar(repo, [a])
    assert (novos, cache) == (1, 0)
    assert repo.count_pending() == 1

    file_id, path = repo.pending_files()[0]
    repo.save_analysis_batch([(file_id, analyze_path(path))])

    novos, cache = _registrar(repo, [a])
    assert (novos, cache) == (0, 1)      # já analisado e inalterado: reaproveita o cache
    assert repo.count_pending() == 0

    make_photo(2, 400, 300).save(a, quality=90)   # arquivo modificado
    novos, cache = _registrar(repo, [a])
    assert (novos, cache) == (1, 0)
    assert repo.count_pending() == 1


def test_salva_e_recarrega_assinaturas(tmp_path, repo):
    a = tmp_path / "a.jpg"
    make_photo(3, 800, 600).save(a, quality=92)
    _registrar(repo, [a])
    pendentes = repo.pending_files()
    assert len(pendentes) == 1

    file_id, path = pendentes[0]
    resultado = analyze_path(path)
    repo.save_analysis_batch([(file_id, resultado)])
    assert repo.count_pending() == 0

    assinaturas = repo.load_signatures()
    assert len(assinaturas) == 1
    sig = assinaturas[0]
    assert sig.sha256 == resultado.sha256
    assert sig.phash == resultado.phash
    assert sig.crop_phash == resultado.crop_phash
    assert sig.gray is not None and sig.gray.shape == (32, 32)
    assert sig.descriptor is not None and sig.descriptor.size == 160
    assert sig.crop_descriptor is not None

    detalhes = repo.photo_details(file_id)
    assert detalhes["quality_report"].score > 0
    assert detalhes["path"] == str(a)


def test_grupos_sobrevivem_a_gravacao_e_leitura(tmp_path, repo):
    import shutil

    a = tmp_path / "a.jpg"
    make_photo(5, 800, 600).save(a, quality=92)
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    _registrar(repo, [a, b])
    resultados = [(fid, analyze_path(path)) for fid, path in repo.pending_files()]
    repo.save_analysis_batch(resultados)

    grupos, _ = build_groups(repo.load_signatures())
    repo.replace_groups(grupos)

    carregados = repo.load_groups()
    assert len(carregados) == 1
    grupo = carregados[0]
    assert grupo.category is Category.EXACT
    assert grupo.size == 2
    assert grupo.reference is not None
    assert repo.count_groups() == 1
    assert repo.count_groups([Category.EXACT.value]) == 1
    assert repo.count_groups([Category.SIMILAR.value]) == 0

    resumo = repo.summary()
    assert resumo["photos"] == 2
    assert resumo["exact_duplicates"] == 1
    assert resumo["reclaimable_bytes"] > 0


def test_escolha_do_usuario_e_preservada_ao_reagrupar(tmp_path, repo):
    import shutil

    a = tmp_path / "a.jpg"
    make_photo(6, 700, 500).save(a, quality=92)
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    _registrar(repo, [a, b])
    repo.save_analysis_batch([(fid, analyze_path(p)) for fid, p in repo.pending_files()])
    grupos, _ = build_groups(repo.load_signatures())
    repo.replace_groups(grupos)

    grupo = repo.load_groups()[0]
    alvo = [m for m in grupo.members if not m.is_reference][0]
    repo.set_member_choice(grupo.group_id, alvo.signature.file_id, "keep")
    assert repo.selected_for_removal() == []

    # Reagrupar não pode apagar a decisão manual.
    grupos, _ = build_groups(repo.load_signatures())
    repo.replace_groups(grupos)
    assert repo.selected_for_removal() == []


def test_troca_da_foto_principal(tmp_path, repo):
    photo = make_photo(7, 900, 700)
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    photo.save(a, quality=95)
    photo.save(b, quality=55)
    _registrar(repo, [a, b])
    repo.save_analysis_batch([(fid, analyze_path(p)) for fid, p in repo.pending_files()])
    grupos, _ = build_groups(repo.load_signatures())
    repo.replace_groups(grupos)

    grupo = repo.load_groups()[0]
    antigo = grupo.reference.signature.file_id
    novo = [m.signature.file_id for m in grupo.members if not m.is_reference][0]
    repo.set_reference(grupo.group_id, novo)

    atualizado = repo.load_group(grupo.group_id)
    assert atualizado.reference.signature.file_id == novo
    assert all(m.recommendation == "keep" or m.signature.file_id != novo for m in atualizado.members)
    assert antigo != novo


def test_decisoes_nao_sao_duplicatas(tmp_path, repo):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    make_photo(8, 500, 400).save(a, quality=90)
    make_photo(9, 500, 400).save(b, quality=90)
    _registrar(repo, [a, b])
    repo.save_analysis_batch([(fid, analyze_path(p)) for fid, p in repo.pending_files()])

    ids = [fid for fid, _ in [(s.file_id, s.path) for s in repo.load_signatures()]]
    assert repo.mark_not_duplicates(ids) == 1
    assert repo.excluded_pairs() == {(min(ids), max(ids))}
    repo.clear_decisions()
    assert repo.excluded_pairs() == set()


def test_migracao_de_esquema_antigo(tmp_path, monkeypatch):
    """Um banco da versão 1 é atualizado sem perder os arquivos já cadastrados."""
    caminho = tmp_path / "antigo.db"
    conexao = sqlite3.connect(caminho)
    conexao.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, folder_id INTEGER,
            name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '', discovered_at TEXT, analyzed_at TEXT);
        CREATE TABLE photos (
            file_id INTEGER PRIMARY KEY, width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0, format TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '',
            phash INTEGER NOT NULL DEFAULT 0, dhash INTEGER NOT NULL DEFAULT 0,
            ahash INTEGER NOT NULL DEFAULT 0, whash INTEGER NOT NULL DEFAULT 0,
            color_sig INTEGER NOT NULL DEFAULT 0, gray BLOB, descriptor BLOB,
            desc_version INTEGER NOT NULL DEFAULT 0, quality REAL NOT NULL DEFAULT 0,
            quality_json TEXT NOT NULL DEFAULT '{}', sharpness REAL NOT NULL DEFAULT 0,
            bpp REAL NOT NULL DEFAULT 0, texture REAL NOT NULL DEFAULT 0,
            exif_json TEXT NOT NULL DEFAULT '{}', has_exif INTEGER NOT NULL DEFAULT 0,
            taken_at TEXT NOT NULL DEFAULT '', capture_key TEXT NOT NULL DEFAULT '',
            camera TEXT NOT NULL DEFAULT '', lens TEXT NOT NULL DEFAULT '', iso INTEGER,
            aperture REAL, shutter TEXT NOT NULL DEFAULT '', gps_lat REAL, gps_lon REAL,
            bad_flags TEXT NOT NULL DEFAULT '', bad_json TEXT NOT NULL DEFAULT '{}',
            thumb TEXT NOT NULL DEFAULT '', analyzed_at TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO files (path, name, ext, status) VALUES ('/fotos/a.jpg', 'a.jpg', '.jpg', 'analyzed');
        INSERT INTO photos (file_id, width, height) VALUES (1, 100, 100);
        """
    )
    conexao.commit()
    conexao.close()

    db = Database(caminho)
    try:
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        colunas = {r["name"] for r in db.query("PRAGMA table_info(photos)")}
        assert {"crop_phash", "crop_dhash", "crop_desc"} <= colunas
        # O arquivo continua cadastrado, mas volta para a fila de análise.
        linha = db.query_one("SELECT status FROM files WHERE path='/fotos/a.jpg'")
        assert linha["status"] == "pending"
    finally:
        db.close()
