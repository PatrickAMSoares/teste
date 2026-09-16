"""Formação dos grupos de duplicatas.

Comparar todas as fotos entre si seria O(n²) - inviável com 100 mil imagens
(5 bilhões de comparações). O caminho usado aqui tem três estágios:

1. **Duplicatas exatas** por SHA-256 (dicionário, custo linear).
2. **Geração de candidatos** por *multi-index hashing*: cada hash de 64 bits é
   dividido em 4 faixas de 16 bits; fotos com distância pequena compartilham
   obrigatoriamente ao menos uma faixa. Somam-se ainda buckets de LSH sobre o
   descritor visual, que capturam recortes e edições mais fortes.
3. **Verificação** dos pares candidatos com :func:`similarity.compare`.

Os grupos finais são formados por *star clustering* em torno da foto de melhor
qualidade, e não por componentes conexas puras - isso evita o efeito de
"corrente" (A parecida com B, B parecida com C, mas A e C são fotos diferentes).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from ..config import Thresholds
from . import embeddings
from .hashing import bands
from .models import Category, Group, Member, PhotoSignature
from .similarity import SimilarityResult, classify, compare, quick_reject

log = logging.getLogger(__name__)

_LSH_BITS = 14
_LSH_TABLES = 3

# Uma foto só é colocada em um grupo se aquele for (praticamente) o seu melhor
# par. Com isso, uma cópia não é "capturada" por uma foto parecida qualquer
# quando existe outra muito mais parecida esperando para formar grupo.
DEFER_MARGIN = 1.5


@dataclass
class GroupingOptions:
    thresholds: Thresholds = field(default_factory=Thresholds)
    strict: bool = True
    detect_crops: bool = True
    use_embeddings: bool = True
    max_candidates_per_file: int = 400
    max_bucket: int = 600
    band_count: int = 4


class UnionFind:
    """Union-find com compressão de caminho."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


@dataclass
class GroupingStats:
    files: int = 0
    candidate_pairs: int = 0
    compared_pairs: int = 0
    accepted_pairs: int = 0
    exact_pairs: int = 0
    groups: int = 0
    elapsed_s: float = 0.0


class GroupBuilder:
    """Constrói os grupos a partir das assinaturas já calculadas."""

    def __init__(
        self,
        signatures: Iterable[PhotoSignature],
        options: GroupingOptions | None = None,
        excluded_pairs: set[tuple[int, int]] | None = None,
    ) -> None:
        self.sigs: list[PhotoSignature] = list(signatures)
        self.opts = options or GroupingOptions()
        self.excluded = excluded_pairs or set()
        self.stats = GroupingStats(files=len(self.sigs))
        self._index_of = {sig.file_id: i for i, sig in enumerate(self.sigs)}
        self._results: dict[tuple[int, int], SimilarityResult] = {}
        self._best_match: dict[int, float] = {}

    # ------------------------------------------------------------- candidatos
    def _candidate_pairs(self) -> list[tuple[int, int]]:
        n = len(self.sigs)
        buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)

        for idx, sig in enumerate(self.sigs):
            for h_id, value in enumerate((sig.phash, sig.dhash, sig.whash, sig.color_sig)):
                for b_id, band in enumerate(bands(value, self.opts.band_count)):
                    buckets[(h_id, b_id, band)].append(idx)
            if self.opts.detect_crops:
                # Os hashes do recorte central entram nos mesmos buckets dos
                # hashes inteiros: assim uma foto recortada encontra a original.
                for h_id, value in ((0, sig.crop_phash), (1, sig.crop_dhash)):
                    if not value:
                        continue
                    for b_id, band in enumerate(bands(value, self.opts.band_count)):
                        buckets[(h_id, b_id, band)].append(idx)

        if self.opts.use_embeddings:
            self._add_lsh_buckets(buckets)

        pairs: set[tuple[int, int]] = set()
        counts = [0] * n
        cap = max(8, self.opts.max_candidates_per_file)

        for members in buckets.values():
            size = len(members)
            if size < 2:
                continue
            if size > self.opts.max_bucket:
                # Bucket enorme: normalmente milhares de imagens quase iguais
                # (fundos lisos). Limitamos para não explodir o custo.
                members = members[: self.opts.max_bucket]
                size = len(members)
            for i in range(size):
                a = members[i]
                if counts[a] >= cap:
                    continue
                for j in range(i + 1, size):
                    b = members[j]
                    if a == b:
                        # A mesma foto pode cair duas vezes no bucket (hash
                        # inteiro e hash do recorte coincidem). Não é um par.
                        continue
                    if counts[b] >= cap or counts[a] >= cap:
                        continue
                    key = (a, b) if a < b else (b, a)
                    if key in pairs:
                        continue
                    pairs.add(key)
                    counts[a] += 1
                    counts[b] += 1

        # Arquivos idênticos sempre entram, independentemente dos limites acima.
        by_sha: dict[str, list[int]] = defaultdict(list)
        for idx, sig in enumerate(self.sigs):
            if sig.sha256:
                by_sha[sig.sha256].append(idx)
        for members in by_sha.values():
            if len(members) < 2:
                continue
            first = members[0]
            for other in members[1:]:
                pairs.add((first, other) if first < other else (other, first))
                self.stats.exact_pairs += 1

        self.stats.candidate_pairs = len(pairs)
        return sorted(pairs)

    def _add_lsh_buckets(self, buckets: dict) -> None:
        """Buckets adicionais por LSH sobre os descritores visuais."""
        dims = None
        for sig in self.sigs:
            if sig.descriptor is not None and sig.descriptor.size:
                dims = int(sig.descriptor.size)
                break
        if not dims:
            return
        for table in range(_LSH_TABLES):
            planes = embeddings.random_hyperplanes(dims, _LSH_BITS, seed=9_100 + table * 977)
            for idx, sig in enumerate(self.sigs):
                for vec in (sig.descriptor, sig.crop_descriptor if self.opts.detect_crops else None):
                    if vec is None or vec.size != dims:
                        continue
                    key = embeddings.lsh_key(np.asarray(vec, dtype=np.float32), planes)
                    buckets[(90 + table, 0, key)].append(idx)

    # ----------------------------------------------------------- verificação
    def _verify(self, pairs: list[tuple[int, int]], progress: Callable[[int, int], None] | None):
        thr = self.opts.thresholds
        accepted: list[tuple[int, int, SimilarityResult]] = []
        total = len(pairs)
        for n, (i, j) in enumerate(pairs):
            a, b = self.sigs[i], self.sigs[j]
            pair_key = (min(a.file_id, b.file_id), max(a.file_id, b.file_id))
            if pair_key in self.excluded:
                continue
            if quick_reject(a, b):
                continue
            result = compare(
                a, b, thresholds=thr, strict=self.opts.strict, detect_crops=self.opts.detect_crops
            )
            self.stats.compared_pairs += 1
            if result.percent >= thr.similar_min:
                self._results[(i, j)] = result
                accepted.append((i, j, result))
                if result.percent > self._best_match.get(i, 0.0):
                    self._best_match[i] = result.percent
                if result.percent > self._best_match.get(j, 0.0):
                    self._best_match[j] = result.percent
            if progress is not None and (n % 2000 == 0 or n == total - 1):
                progress(n + 1, total)
        self.stats.accepted_pairs = len(accepted)
        return accepted

    def _similarity(self, i: int, j: int) -> SimilarityResult:
        key = (i, j) if i < j else (j, i)
        cached = self._results.get(key)
        if cached is None:
            cached = compare(
                self.sigs[key[0]],
                self.sigs[key[1]],
                thresholds=self.opts.thresholds,
                strict=self.opts.strict,
                detect_crops=self.opts.detect_crops,
            )
            self._results[key] = cached
        return cached

    # ----------------------------------------------------------------- grupos
    def build(self, progress: Callable[[int, int], None] | None = None) -> list[Group]:
        """Monta os grupos em cascata, do critério mais rígido para o mais frouxo.

        Cada foto é atribuída a **um** grupo: o do nível mais rígido em que ela
        se encaixa. A foto de referência de um grupo já formado pode aparecer
        como âncora em um grupo de nível inferior (por exemplo, um recorte que
        se parece com a foto mantida), mas nunca é recomendada para remoção.
        """
        started = time.perf_counter()
        if len(self.sigs) < 2:
            self.stats.elapsed_s = time.perf_counter() - started
            return []

        pairs = self._candidate_pairs()
        accepted = self._verify(pairs, progress)

        adjacency: dict[int, set[int]] = defaultdict(set)
        for i, j, _result in accepted:
            adjacency[i].add(j)
            adjacency[j].add(i)

        thr = self.opts.thresholds
        groups: list[Group] = []
        assigned: set[int] = set()
        anchors: list[int] = []

        # Passo 0: duplicatas exatas (SHA-256 idêntico) - sem margem para dúvida.
        for cluster in self._exact_clusters():
            group = self._make_group(len(groups) + 1, cluster, Category.EXACT, set())
            if group is not None:
                groups.append(group)
                assigned.update(cluster)
                anchors.append(cluster[0] if len(cluster) == 1 else self._best(cluster))

        # Passos seguintes: duplicata visual -> muito semelhante -> semelhante.
        tiers = [
            (thr.duplicate_min, Category.VISUAL),
            (thr.very_similar_min, Category.VERY_SIMILAR),
            (thr.similar_min, Category.SIMILAR),
        ]
        for threshold, category in tiers:
            for cluster, fresh in self._cluster_tier(adjacency, threshold, assigned, anchors):
                group = self._make_group(len(groups) + 1, cluster, category, fresh)
                if group is None:
                    continue
                groups.append(group)
                assigned.update(fresh)
                anchors.append(self._best(cluster))

        groups.sort(key=lambda g: (g.category.order, -g.wasted_bytes, -g.size))
        for position, group in enumerate(groups, start=1):
            group.group_id = position
        self.stats.groups = len(groups)
        self.stats.elapsed_s = time.perf_counter() - started
        return groups

    def _exact_clusters(self) -> list[list[int]]:
        by_sha: dict[str, list[int]] = defaultdict(list)
        for idx, sig in enumerate(self.sigs):
            if sig.sha256:
                by_sha[sig.sha256].append(idx)
        return [sorted(v, key=lambda i: _rank_key(self.sigs[i]), reverse=True) for v in by_sha.values() if len(v) > 1]

    def _cluster_tier(
        self,
        adjacency: dict[int, set[int]],
        threshold: float,
        assigned: set[int],
        anchors: list[int],
    ) -> list[tuple[list[int], set[int]]]:
        """Star clustering em um nível de similaridade.

        Retorna pares ``(cluster, membros_novos)``. O agrupamento parte sempre da
        foto de melhor qualidade (a "estrela") e só entram no grupo as fotos
        comparadas **diretamente com ela** - é isso que evita o efeito de corrente,
        em que A ~ B e B ~ C arrastariam C para junto de A sem que sejam parecidas.

        Cada grupo pode receber no máximo uma âncora (a foto de referência de um
        grupo já formado em nível mais rígido), o que mantém o resultado legível.
        """
        anchor_set = {a for a in anchors if adjacency.get(a)}
        pending = {i for i in range(len(self.sigs)) if i not in assigned and adjacency.get(i)}
        order = sorted(pending, key=lambda i: (-self.sigs[i].quality, -self.sigs[i].megapixels, i))

        clusters: list[tuple[list[int], set[int]]] = []
        used_anchors: set[int] = set()
        deferred: set[int] = set()
        strict_keys = self.opts.strict and threshold >= self.opts.thresholds.duplicate_min

        for seed in order:
            if seed not in pending:
                continue
            pending.discard(seed)
            cluster = [seed]
            fresh = {seed}
            anchor_used: int | None = None
            cluster_keys = {self.sigs[seed].capture_key} - {""}
            neighbours = sorted(adjacency.get(seed, ()), key=lambda i: (-self.sigs[i].quality, i))
            for other in neighbours:
                is_pending = other in pending
                is_free_anchor = other in anchor_set and other not in used_anchors and anchor_used is None
                if not is_pending and not is_free_anchor:
                    continue
                percent = self._similarity(seed, other).percent
                if percent < threshold:
                    continue

                # Instantes de captura conflitantes dentro do grupo: são fotos
                # diferentes da mesma cena (rajada), nunca duplicatas.
                other_key = self.sigs[other].capture_key
                if strict_keys and other_key and cluster_keys and other_key not in cluster_keys:
                    continue

                # Existe um par bem melhor para esta foto? Então deixamos que
                # ela forme grupo com ele, em vez de prendê-la aqui.
                if (
                    is_pending
                    and other not in deferred
                    and percent + DEFER_MARGIN < self._best_match.get(other, 0.0)
                ):
                    deferred.add(other)
                    continue

                cluster.append(other)
                if is_pending:
                    pending.discard(other)
                    fresh.add(other)
                    if other_key:
                        cluster_keys.add(other_key)
                else:
                    anchor_used = other
            if len(cluster) > 1:
                if anchor_used is not None:
                    used_anchors.add(anchor_used)
                clusters.append((cluster, fresh))
            else:
                # Sem parceiros neste nível: a foto volta a ficar disponível para
                # o próximo nível, mais tolerante.
                pending.add(seed)
        return clusters

    def _best(self, cluster: list[int]) -> int:
        return max(cluster, key=lambda i: _rank_key(self.sigs[i]))

    def _make_group(
        self, group_id: int, cluster: list[int], category: Category, fresh: set[int]
    ) -> Group | None:
        if len(cluster) < 2:
            return None
        ranked = sorted(cluster, key=lambda i: _rank_key(self.sigs[i]), reverse=True)
        ref_idx = ranked[0]
        members: list[Member] = []
        sims: list[float] = []

        for idx in ranked:
            if idx == ref_idx:
                members.append(
                    Member(signature=self.sigs[idx], similarity=100.0, is_reference=True, recommendation="keep")
                )
                continue
            result = self._similarity(ref_idx, idx)
            sims.append(result.percent)
            members.append(
                Member(
                    signature=self.sigs[idx],
                    similarity=result.percent,
                    is_reference=False,
                    recommendation="keep",
                    detail=result.to_dict(),
                )
            )
        if len(members) < 2:
            return None

        # Rede de segurança: se o grupo reúne fotos com instantes de captura
        # distintos, elas não são versões do mesmo arquivo - são fotos
        # diferentes. O grupo é rebaixado e nada é sugerido para remoção.
        if self.opts.strict and category in (Category.VISUAL,):
            keys = {self.sigs[i].capture_key for i in cluster if self.sigs[i].capture_key}
            if len(keys) > 1:
                category = Category.VERY_SIMILAR

        # Recomendação conservadora: só sugerimos remover em duplicatas (exatas
        # ou visuais). Em "muito semelhante"/"semelhante" a decisão é do usuário.
        if category in (Category.EXACT, Category.VISUAL):
            for member in members:
                if member.is_reference:
                    continue
                if fresh and self._index_of.get(member.signature.file_id) not in fresh:
                    member.reason = "Já é a foto recomendada de outro grupo - mantida por segurança."
                    continue  # âncora de outro grupo: nunca sugerimos remover
                member.recommendation = "remove"
                member.reason = (
                    "Cópia idêntica do arquivo recomendado (mesmo conteúdo byte a byte)."
                    if category is Category.EXACT
                    else "Mesma fotografia, porém com qualidade técnica inferior."
                )
        else:
            for member in members:
                if member.is_reference:
                    continue
                if fresh and self._index_of.get(member.signature.file_id) not in fresh:
                    member.reason = "Já é a foto recomendada de outro grupo - mantida por segurança."
                else:
                    member.reason = "Revise manualmente: há diferenças que podem ser importantes."

        reference = members[0]
        reference.reason = _reference_reason(reference, members[1:])
        return Group(group_id=group_id, category=category, members=members)


def _rank_key(sig: PhotoSignature) -> tuple:
    """Critério de escolha da melhor foto do grupo."""
    return (round(sig.quality, 2), sig.width * sig.height, sig.size, -len(sig.path))


def _reference_reason(reference: Member, others: list[Member]) -> str:
    sig = reference.signature
    best_res = all(sig.width * sig.height >= o.signature.width * o.signature.height for o in others)
    best_size = all(sig.size >= o.signature.size for o in others)
    bits = [f"qualidade {sig.quality:.0f}/100"]
    if best_res:
        bits.append(f"maior resolução ({sig.width} × {sig.height})")
    if best_size and not best_res:
        bits.append("arquivo mais completo")
    return "Recomendada para manter: " + ", ".join(bits) + "."


def build_groups(
    signatures: Iterable[PhotoSignature],
    options: GroupingOptions | None = None,
    excluded_pairs: set[tuple[int, int]] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Group], GroupingStats]:
    """Atalho funcional para :class:`GroupBuilder`."""
    builder = GroupBuilder(signatures, options, excluded_pairs)
    groups = builder.build(progress)
    return groups, builder.stats
