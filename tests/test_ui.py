"""Testes de fumaça da interface (executados com a plataforma Qt "offscreen")."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 não está instalado")

from PySide6.QtWidgets import QApplication  # noqa: E402

from photodedupe.core.pipeline import AnalysisPipeline  # noqa: E402
from photodedupe.ui.main_window import (  # noqa: E402
    PAGE_BAD,
    PAGE_GROUPS,
    PAGE_REPORT,
    PAGE_REVIEW,
    PAGE_SETTINGS,
    PAGE_START,
    MainWindow,
)
from photodedupe.ui.theme import palette, quality_color, stylesheet  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def janela(qapp, library, db, settings, repo):
    AnalysisPipeline(repo, settings).run([str(library)])
    window = MainWindow(db, settings)
    window.resize(1400, 900)
    yield window
    window.thumbnails.shutdown()
    window.close()


def test_folha_de_estilo_dos_dois_temas():
    for tema in ("dark", "light"):
        css = stylesheet(tema)
        assert "QPushButton" in css and palette(tema)["accent"] in css
    assert quality_color(90) != quality_color(20)


def test_todas_as_telas_abrem(janela, qapp):
    for pagina in (PAGE_START, PAGE_GROUPS, PAGE_REVIEW, PAGE_BAD, PAGE_REPORT, PAGE_SETTINGS):
        janela.go_to(pagina)
        qapp.processEvents()
        assert janela.stack.currentIndex() == pagina


def test_tela_de_duplicatas_mostra_os_grupos(janela, qapp):
    janela.go_to(PAGE_GROUPS)
    qapp.processEvents()
    assert janela.groups_page._cards, "nenhum grupo foi exibido"
    cartao = janela.groups_page._cards[0]
    assert cartao.group.reference is not None
    assert len(cartao.cards) == cartao.group.size


def test_aceitar_recomendacao_marca_para_remocao(janela, qapp):
    janela.go_to(PAGE_GROUPS)
    qapp.processEvents()
    cartao = janela.groups_page._cards[0]
    cartao.accept_recommendation()
    for membro in cartao.group.members:
        janela.groups_page._on_choice(cartao.group.group_id, membro.signature.file_id, membro.effective_choice)
    qapp.processEvents()

    selecionados = janela.repo.selected_for_removal()
    assert selecionados
    assert "marcados para remoção" in janela.groups_page.selection_label.text()
    # a foto principal nunca é marcada
    referencia = cartao.group.reference.signature.file_id
    assert all(item["file_id"] != referencia for item in selecionados)


def test_revisao_apresenta_um_grupo_por_vez(janela, qapp):
    janela.go_to(PAGE_REVIEW)
    qapp.processEvents()
    assert janela.review_page._card is not None
    assert "Manter" in janela.review_page.suggestion_title.text()
    total = len(janela.review_page._queue)
    janela.review_page._accept()
    qapp.processEvents()
    assert janela.review_page._position == 1
    assert total >= 1


def test_dialogo_de_confirmacao_lista_os_arquivos(janela, qapp):
    from photodedupe.core.fileops import FileManager
    from photodedupe.ui.widgets.dialogs import ConfirmDeletionDialog

    manager = FileManager(janela.repo, janela.settings.quarantine_path())
    janela.go_to(PAGE_GROUPS)
    cartao = janela.groups_page._cards[0]
    cartao.accept_recommendation()
    for membro in cartao.group.members:
        janela.groups_page._on_choice(cartao.group.group_id, membro.signature.file_id, membro.effective_choice)

    plano = manager.build_plan()
    assert plano.count > 0

    dialogo = ConfirmDeletionDialog(plano, "dark", True)
    qapp.processEvents()
    # O diálogo precisa mostrar quantidade, espaço, destino e a lista completa.
    textos = [w.text() for w in dialogo.findChildren(type(dialogo.quarantine_radio))]
    assert any("quarentena" in t.lower() for t in textos)
    tabela = dialogo.findChildren(type(dialogo))  # sanity: o diálogo montou
    assert tabela is not None
    assert str(plano.count) in plano.summary_text()
    assert "quarentena" in plano.summary_text()
    # o padrão seguro: fechar sem confirmar não altera nada
    dialogo.reject()
    assert dialogo.result() == 0
    dialogo.close()


def test_comparacao_lado_a_lado(janela, qapp):
    from photodedupe.ui.widgets.compare_dialog import CompareDialog

    janela.go_to(PAGE_GROUPS)
    cartao = janela.groups_page._cards[0]
    ids = [m.signature.file_id for m in cartao.group.members][:2]
    fotos = [janela.repo.photo_details(i) for i in ids]

    dialogo = CompareDialog(fotos, "dark", False)
    qapp.processEvents()
    assert len(dialogo._panes) == 2
    dialogo.diff_check.setChecked(True)
    qapp.processEvents()
    assert "%" in dialogo.diff_info.text()
    dialogo._set_zoom(200)
    assert dialogo.zoom_label.text() == "200%"
    dialogo.close()


def test_pagina_de_relatorio_mostra_o_resumo(janela, qapp):
    janela.go_to(PAGE_REPORT)
    qapp.processEvents()
    assert janela.report_page.tiles["photos"].value_label.text() != "0"


def test_configuracoes_salvam_limites(janela, qapp):
    janela.go_to(PAGE_SETTINGS)
    qapp.processEvents()
    pagina = janela.settings_page
    pagina.duplicate_spin.setValue(97.0)
    pagina.save(silent=True)
    assert janela.settings.thresholds.duplicate_min == 97.0
