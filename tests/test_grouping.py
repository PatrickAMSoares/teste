"""Testes da formação dos grupos (cascata de níveis e star clustering)."""

from __future__ import annotations

import shutil

from conftest import exif_bytes, make_photo

from photodedupe.core.analyzer import analyze_path, signature_from_result
from photodedupe.core.grouping import GroupingOptions, UnionFind, build_groups
from photodedupe.core.models import Category


def _sigs(paths):
    return [signature_from_result(i + 1, analyze_path(str(p))) for i, p in enumerate(paths)]


def test_union_find_basico():
    uf = UnionFind(5)
    uf.union(0, 1)
    uf.union(1, 2)
    assert uf.find(0) == uf.find(2)
    assert uf.find(0) != uf.find(3)


def test_copias_exatas_formam_grupo_proprio(tmp_path):
    a = tmp_path / "a.jpg"
    make_photo(2, 700, 500).save(a, quality=92, exif=exif_bytes())
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    c = tmp_path / "c.jpg"
    make_photo(300, 700, 500).save(c, quality=92)

    grupos, stats = build_groups(_sigs([a, b, c]))
    assert len(grupos) == 1
    grupo = grupos[0]
    assert grupo.category is Category.EXACT
    assert grupo.size == 2
    assert stats.files == 3
    # a foto diferente ficou de fora
    assert all("c.jpg" not in m.signature.path for m in grupo.members)


def test_cascata_separa_duplicatas_de_semelhantes(tmp_path):
    photo = make_photo(12, 1000, 750)
    original = tmp_path / "original.jpg"
    photo.save(original, quality=95, exif=exif_bytes())
    recomprimida = tmp_path / "recomprimida.jpg"
    photo.save(recomprimida, quality=45, exif=exif_bytes())
    sequencia = tmp_path / "sequencia.jpg"
    make_photo(12, 1000, 750, variant=3).save(
        sequencia, quality=95, exif=exif_bytes("2024:05:01 10:00:05", "80")
    )

    grupos, _ = build_groups(_sigs([original, recomprimida, sequencia]))
    por_categoria = {g.category: g for g in grupos}
    assert Category.VISUAL in por_categoria
    duplicatas = por_categoria[Category.VISUAL]
    caminhos = {m.signature.path for m in duplicatas.members}
    assert str(original) in caminhos and str(recomprimida) in caminhos
    assert str(sequencia) not in caminhos          # a foto em sequência não entra
    # e a sequência, se aparecer, é em nível mais frouxo
    for grupo in grupos:
        if grupo is duplicatas:
            continue
        assert grupo.category in (Category.VERY_SIMILAR, Category.SIMILAR)


def test_melhor_foto_e_a_referencia_e_recebe_recomendacao(tmp_path):
    photo = make_photo(15, 1600, 1200)
    boa = tmp_path / "boa.jpg"
    photo.save(boa, quality=96, exif=exif_bytes())
    ruim = tmp_path / "ruim.jpg"
    photo.resize((640, 480)).save(ruim, quality=35)

    grupos, _ = build_groups(_sigs([ruim, boa]))
    assert len(grupos) == 1
    grupo = grupos[0]
    referencia = grupo.reference
    assert referencia is not None
    assert referencia.signature.path == str(boa)
    assert referencia.recommendation == "keep"
    assert "Recomendada para manter" in referencia.reason
    outro = [m for m in grupo.members if not m.is_reference][0]
    assert outro.recommendation == "remove"
    assert grupo.wasted_bytes == outro.signature.size


def test_decisao_do_usuario_impede_novo_agrupamento(tmp_path):
    photo = make_photo(18, 800, 600)
    a = tmp_path / "a.jpg"
    photo.save(a, quality=95)
    b = tmp_path / "b.jpg"
    photo.save(b, quality=60)

    assinaturas = _sigs([a, b])
    grupos, _ = build_groups(assinaturas)
    assert len(grupos) == 1

    par = (assinaturas[0].file_id, assinaturas[1].file_id)
    grupos, _ = build_groups(assinaturas, excluded_pairs={par})
    assert grupos == []


def test_sem_efeito_corrente_entre_fotos_distintas(tmp_path):
    """A ~ B e B ~ C não podem arrastar C para o grupo de A."""
    caminhos = []
    for variante in range(3):
        path = tmp_path / f"cena_{variante}.jpg"
        make_photo(25, 900, 700, variant=variante * 6).save(path, quality=94)
        caminhos.append(path)

    grupos, _ = build_groups(_sigs(caminhos), GroupingOptions())
    for grupo in grupos:
        # Cada grupo só contém fotos comparadas diretamente com a referência.
        referencia = grupo.reference
        for membro in grupo.members:
            if membro.is_reference:
                continue
            assert membro.similarity >= 75.0, (
                f"{membro.signature.name} entrou com apenas {membro.similarity:.1f}%"
            )


def test_grupos_vem_ordenados_por_categoria(tmp_path):
    photo = make_photo(31, 900, 700)
    exata_a = tmp_path / "exata_a.jpg"
    photo.save(exata_a, quality=93, exif=exif_bytes())
    exata_b = tmp_path / "exata_b.jpg"
    shutil.copy2(exata_a, exata_b)

    outra = make_photo(45, 900, 700)
    visual_a = tmp_path / "visual_a.jpg"
    outra.save(visual_a, quality=95, exif=exif_bytes("2024:06:01 08:00:00"))
    visual_b = tmp_path / "visual_b.png"
    outra.save(visual_b)

    grupos, _ = build_groups(_sigs([exata_a, exata_b, visual_a, visual_b]))
    categorias = [g.category for g in grupos]
    assert categorias == sorted(categorias, key=lambda c: c.order)
    assert Category.EXACT in categorias and Category.VISUAL in categorias


def test_copia_fica_com_a_foto_de_origem_e_nao_com_a_parecida(tmp_path):
    """Cenário real: uma rajada (foto parecida) não pode "roubar" as cópias da original.

    A original tem cópias em PNG/WEBP sem EXIF. Uma segunda foto da mesma cena
    (tirada segundos depois) é tecnicamente parecidíssima com essas cópias. O
    agrupamento precisa manter cada cópia junto da sua verdadeira origem.
    """
    photo = make_photo(64, 1200, 900)
    original = tmp_path / "IMG_0064.jpg"
    photo.save(original, quality=95, exif=exif_bytes("2024:07:01 09:00:00", "11"))
    copia_png = tmp_path / "IMG_0064.png"
    photo.save(copia_png)                                   # sem EXIF
    copia_webp = tmp_path / "IMG_0064.webp"
    photo.save(copia_webp, quality=85)                      # sem EXIF
    sequencia = tmp_path / "IMG_0064_1.jpg"
    make_photo(64, 1200, 900, variant=1).save(
        sequencia, quality=96, exif=exif_bytes("2024:07:01 09:00:03", "77")
    )

    grupos, _ = build_groups(_sigs([sequencia, copia_png, copia_webp, original]))
    duplicatas = [g for g in grupos if g.category in (Category.EXACT, Category.VISUAL)]
    assert duplicatas, "as cópias deveriam formar um grupo de duplicatas"

    for grupo in duplicatas:
        caminhos = {m.signature.path for m in grupo.members}
        assert str(sequencia) not in caminhos, "a foto em sequência entrou em um grupo de duplicatas"
        if str(copia_png) in caminhos or str(copia_webp) in caminhos:
            assert str(original) in caminhos, "a cópia ficou separada da sua origem"

    # A foto em sequência nunca pode ser recomendada para remoção.
    for grupo in grupos:
        for membro in grupo.members:
            if membro.signature.path == str(sequencia):
                assert membro.recommendation == "keep"
