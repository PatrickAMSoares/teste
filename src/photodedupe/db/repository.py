"""Acesso aos dados: arquivos, fotos, grupos, decisões e histórico de ações."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from ..core.embeddings import DESCRIPTOR_VERSION
from ..core.exif import ExifData
from ..core.hashing import from_signed, to_signed
from ..core.models import (
    AnalysisResult,
    Category,
    FileStatus,
    Group,
    Member,
    PhotoSignature,
    QualityReport,
)
from ..paths import thumbs_dir
from .database import Database

log = logging.getLogger(__name__)


class Repository:
    """Todas as consultas do aplicativo em um único lugar."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ pastas
    def add_folder(self, path: str, recursive: bool = True) -> int:
        normalized = str(Path(path).resolve())
        self.db.execute(
            "INSERT INTO folders(path, recursive) VALUES(?, ?) "
            "ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive, enabled=1",
            (normalized, 1 if recursive else 0),
        )
        self.db.commit()
        row = self.db.query_one("SELECT id FROM folders WHERE path=?", (normalized,))
        return int(row["id"]) if row else 0

    def list_folders(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM folders WHERE enabled=1 ORDER BY path")]

    def remove_folder(self, folder_id: int, delete_files: bool = True) -> None:
        if delete_files:
            self.db.execute("DELETE FROM files WHERE folder_id=?", (folder_id,))
        self.db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        self.db.commit()

    # ---------------------------------------------------------------- arquivos
    def register_files(self, entries: Iterable[tuple[str, int, int, int | None]]) -> tuple[int, int]:
        """Registra arquivos descobertos.

        ``entries`` é uma sequência de ``(caminho, tamanho, mtime_ns, folder_id)``.
        Arquivos já analisados e inalterados mantêm o resultado em cache
        (análise incremental). Retorna ``(novos_ou_alterados, inalterados)``.
        """
        new_or_changed = 0
        unchanged = 0
        with self.db.lock:
            conn = self.db.connection
            cur = conn.cursor()
            for path, size, mtime_ns, folder_id in entries:
                p = Path(path)
                row = cur.execute("SELECT id, size, mtime_ns, status FROM files WHERE path=?", (path,)).fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO files(path, folder_id, name, ext, size, mtime_ns, status) "
                        "VALUES(?, ?, ?, ?, ?, ?, 'pending')",
                        (path, folder_id, p.name, p.suffix.lower(), size, mtime_ns),
                    )
                    new_or_changed += 1
                elif row["size"] != size or row["mtime_ns"] != mtime_ns or row["status"] == FileStatus.PENDING.value:
                    cur.execute(
                        "UPDATE files SET size=?, mtime_ns=?, status='pending', error='', folder_id=COALESCE(?, folder_id) WHERE id=?",
                        (size, mtime_ns, folder_id, row["id"]),
                    )
                    cur.execute("DELETE FROM photos WHERE file_id=?", (row["id"],))
                    new_or_changed += 1
                else:
                    if folder_id is not None and row["status"] != FileStatus.REMOVED.value:
                        cur.execute("UPDATE files SET folder_id=? WHERE id=?", (folder_id, row["id"]))
                    unchanged += 1
            conn.commit()
            cur.close()
        return new_or_changed, unchanged

    def pending_files(self, limit: int | None = None) -> list[tuple[int, str]]:
        sql = "SELECT id, path FROM files WHERE status='pending' ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(int(r["id"]), r["path"]) for r in self.db.query(sql)]

    def count_pending(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM files WHERE status='pending'")
        return int(row["n"]) if row else 0

    def count_files(self, status: str | None = None) -> int:
        if status:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM files WHERE status=?", (status,))
        else:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM files")
        return int(row["n"]) if row else 0

    def file_id_for_path(self, path: str) -> int | None:
        row = self.db.query_one("SELECT id FROM files WHERE path=?", (path,))
        return int(row["id"]) if row else None

    def mark_missing(self, file_ids: Sequence[int]) -> None:
        if not file_ids:
            return
        self.db.executemany("UPDATE files SET status='missing' WHERE id=?", [(int(i),) for i in file_ids])
        self.db.commit()

    def mark_removed(self, file_ids: Sequence[int]) -> None:
        if not file_ids:
            return
        self.db.executemany("UPDATE files SET status='removed' WHERE id=?", [(int(i),) for i in file_ids])
        self.db.commit()

    def restore_status(self, file_ids: Sequence[int], status: str = FileStatus.ANALYZED.value) -> None:
        if not file_ids:
            return
        self.db.executemany("UPDATE files SET status=? WHERE id=?", [(status, int(i)) for i in file_ids])
        self.db.commit()

    # ---------------------------------------------------------------- análise
    def save_analysis_batch(self, results: Sequence[tuple[int, AnalysisResult]]) -> None:
        """Grava em lote o resultado da análise (uma transação por lote)."""
        photo_rows = []
        file_rows = []
        for file_id, res in results:
            if not res.ok:
                file_rows.append((FileStatus.ERROR.value, res.error[:500], file_id))
                continue
            thumb_path = ""
            if res.thumb_bytes:
                thumb_path = self.store_thumbnail(res.sha256 or str(file_id), res.thumb_bytes)
            exif = res.exif
            photo_rows.append(
                (
                    file_id, res.width, res.height, res.format, res.mode, res.sha256,
                    to_signed(res.phash), to_signed(res.dhash), to_signed(res.ahash),
                    to_signed(res.whash), to_signed(res.color_sig),
                    to_signed(res.crop_phash), to_signed(res.crop_dhash),
                    res.gray_blob or None, res.desc_blob or None, res.crop_desc_blob or None,
                    DESCRIPTOR_VERSION,
                    float(res.quality.score), json.dumps(res.quality.to_dict(), ensure_ascii=False),
                    float(res.quality.sharpness_raw), float(res.quality.bits_per_pixel), float(res.texture),
                    json.dumps(_exif_payload(exif), ensure_ascii=False), 1 if exif.present else 0,
                    exif.taken_at, exif.capture_key(), exif.camera_label(), exif.lens,
                    exif.iso, exif.aperture, exif.shutter, exif.gps_lat, exif.gps_lon,
                    ",".join(res.bad_flags), json.dumps(res.bad_details, ensure_ascii=False), thumb_path,
                )
            )
            file_rows.append((FileStatus.ANALYZED.value, "", file_id))

        with self.db.lock:
            conn = self.db.connection
            conn.executemany(
                """INSERT INTO photos(
                       file_id, width, height, format, mode, sha256,
                       phash, dhash, ahash, whash, color_sig, crop_phash, crop_dhash,
                       gray, descriptor, crop_desc, desc_version,
                       quality, quality_json, sharpness, bpp, texture,
                       exif_json, has_exif, taken_at, capture_key, camera, lens,
                       iso, aperture, shutter, gps_lat, gps_lon,
                       bad_flags, bad_json, thumb, analyzed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(file_id) DO UPDATE SET
                       width=excluded.width, height=excluded.height, format=excluded.format,
                       mode=excluded.mode, sha256=excluded.sha256, phash=excluded.phash,
                       dhash=excluded.dhash, ahash=excluded.ahash, whash=excluded.whash,
                       color_sig=excluded.color_sig, crop_phash=excluded.crop_phash,
                       crop_dhash=excluded.crop_dhash, gray=excluded.gray, descriptor=excluded.descriptor,
                       crop_desc=excluded.crop_desc, desc_version=excluded.desc_version, quality=excluded.quality,
                       quality_json=excluded.quality_json, sharpness=excluded.sharpness,
                       bpp=excluded.bpp, texture=excluded.texture, exif_json=excluded.exif_json,
                       has_exif=excluded.has_exif, taken_at=excluded.taken_at,
                       capture_key=excluded.capture_key, camera=excluded.camera, lens=excluded.lens,
                       iso=excluded.iso, aperture=excluded.aperture, shutter=excluded.shutter,
                       gps_lat=excluded.gps_lat, gps_lon=excluded.gps_lon, bad_flags=excluded.bad_flags,
                       bad_json=excluded.bad_json, thumb=excluded.thumb, analyzed_at=datetime('now')""",
                photo_rows,
            )
            conn.executemany(
                "UPDATE files SET status=?, error=?, analyzed_at=datetime('now') WHERE id=?", file_rows
            )
            conn.commit()

    def store_thumbnail(self, key: str, data: bytes) -> str:
        """Grava a miniatura no cache do aplicativo (nunca junto das fotos)."""
        safe = "".join(c for c in key if c.isalnum())[:40] or "thumb"
        folder = thumbs_dir() / safe[:2]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{safe}.jpg"
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        return str(target)

    # ------------------------------------------------------------ assinaturas
    def load_signatures(self, only_analyzed: bool = True) -> list[PhotoSignature]:
        """Carrega as assinaturas em arrays contíguos (economiza muita memória).

        Para 100 mil fotos o custo fica em torno de 170 MB: 1 KB por imagem de
        assinatura em tons de cinza + 640 B de descritor.
        """
        sql = (
            "SELECT p.file_id, f.path, f.size, p.width, p.height, p.format, p.sha256, "
            "       p.phash, p.dhash, p.ahash, p.whash, p.color_sig, p.crop_phash, p.crop_dhash, "
            "       p.gray, p.descriptor, p.crop_desc, "
            "       p.quality, p.texture, p.taken_at, p.capture_key, p.camera "
            "FROM photos p JOIN files f ON f.id = p.file_id "
        )
        if only_analyzed:
            sql += "WHERE f.status='analyzed' "
        sql += "ORDER BY p.file_id"
        rows = self.db.query(sql)
        n = len(rows)
        if n == 0:
            return []

        gray_matrix = np.zeros((n, 32, 32), dtype=np.uint8)
        desc_len = 0
        for row in rows:
            if row["descriptor"]:
                desc_len = len(row["descriptor"]) // 2
                break
        desc_matrix = np.zeros((n, desc_len), dtype=np.float32) if desc_len else None
        crop_matrix = np.zeros((n, desc_len), dtype=np.float32) if desc_len else None

        signatures: list[PhotoSignature] = []
        for i, row in enumerate(rows):
            gray_blob = row["gray"]
            gray_view = None
            if gray_blob and len(gray_blob) == 1024:
                gray_matrix[i] = np.frombuffer(gray_blob, dtype=np.uint8).reshape(32, 32)
                gray_view = gray_matrix[i]
            desc_view = None
            if desc_matrix is not None and row["descriptor"] and len(row["descriptor"]) // 2 == desc_len:
                desc_matrix[i] = np.frombuffer(row["descriptor"], dtype=np.float16).astype(np.float32)
                desc_view = desc_matrix[i]
            crop_view = None
            if crop_matrix is not None and row["crop_desc"] and len(row["crop_desc"]) // 2 == desc_len:
                crop_matrix[i] = np.frombuffer(row["crop_desc"], dtype=np.float16).astype(np.float32)
                crop_view = crop_matrix[i]
            signatures.append(
                PhotoSignature(
                    file_id=int(row["file_id"]),
                    path=row["path"],
                    size=int(row["size"] or 0),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    format=row["format"],
                    sha256=row["sha256"],
                    phash=from_signed(int(row["phash"])),
                    dhash=from_signed(int(row["dhash"])),
                    ahash=from_signed(int(row["ahash"])),
                    whash=from_signed(int(row["whash"])),
                    color_sig=from_signed(int(row["color_sig"])),
                    crop_phash=from_signed(int(row["crop_phash"] or 0)),
                    crop_dhash=from_signed(int(row["crop_dhash"] or 0)),
                    gray=gray_view,
                    descriptor=desc_view,
                    crop_descriptor=crop_view,
                    quality=float(row["quality"]),
                    texture=float(row["texture"]),
                    taken_at=row["taken_at"],
                    capture_key=row["capture_key"],
                    camera=row["camera"],
                )
            )
        return signatures

    def photo_details(self, file_id: int) -> dict | None:
        row = self.db.query_one(
            "SELECT p.*, f.path, f.size, f.name, f.ext, f.status, f.mtime_ns, f.discovered_at "
            "FROM photos p JOIN files f ON f.id=p.file_id WHERE p.file_id=?",
            (file_id,),
        )
        if row is None:
            return None
        data = dict(row)
        data["quality_report"] = QualityReport.from_dict(json.loads(data.get("quality_json") or "{}"))
        data["exif"] = json.loads(data.get("exif_json") or "{}")
        data["bad_details"] = json.loads(data.get("bad_json") or "{}")
        data["bad_flags"] = [f for f in (data.get("bad_flags") or "").split(",") if f]
        return data

    # ------------------------------------------------------------------ grupos
    def replace_groups(self, groups: Sequence[Group]) -> None:
        """Substitui os grupos, preservando as escolhas manuais já feitas."""
        choices_by_file = {
            int(row["file_id"]): row["user_choice"]
            for row in self.db.query("SELECT file_id, user_choice FROM group_members WHERE user_choice IS NOT NULL")
        }

        with self.db.lock:
            conn = self.db.connection
            conn.execute("DELETE FROM group_members")
            conn.execute("DELETE FROM groups")
            for group in groups:
                ref = group.reference
                conn.execute(
                    "INSERT INTO groups(id, category, size, total_bytes, wasted_bytes, min_sim, reference_id, status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        group.group_id, group.category.value, group.size, group.total_bytes,
                        group.wasted_bytes, group.min_similarity,
                        ref.signature.file_id if ref else None, group.status,
                    ),
                )
                conn.executemany(
                    "INSERT INTO group_members(group_id, file_id, similarity, is_reference, recommendation, reason, user_choice, detail_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            group.group_id, m.signature.file_id, m.similarity, 1 if m.is_reference else 0,
                            m.recommendation, m.reason,
                            choices_by_file.get(m.signature.file_id),
                            json.dumps(m.detail, ensure_ascii=False),
                        )
                        for m in group.members
                    ],
                )
            conn.commit()

    def load_groups(
        self,
        categories: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str = "wasted_desc",
        filters: dict | None = None,
    ) -> list[Group]:
        where = []
        params: list = []
        if categories:
            where.append(f"g.category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        if statuses:
            where.append(f"g.status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        member_sql, member_params = _member_filter_sql(filters)
        if member_sql:
            where.append(member_sql)
            params.extend(member_params)
        order_sql = {
            "wasted_desc": "g.wasted_bytes DESC, g.id",
            "wasted_asc": "g.wasted_bytes ASC, g.id",
            "similarity_desc": "g.min_sim DESC, g.id",
            "similarity_asc": "g.min_sim ASC, g.id",
            "size_desc": "g.size DESC, g.id",
            "category": "g.category, g.id",
            "id": "g.id",
        }.get(order, "g.wasted_bytes DESC, g.id")

        sql = "SELECT g.* FROM groups g"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_sql}"
        if limit:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        group_rows = self.db.query(sql, params)
        if not group_rows:
            return []

        ids = [int(r["id"]) for r in group_rows]
        placeholders = ",".join("?" * len(ids))
        member_rows = self.db.query(
            "SELECT gm.*, f.path, f.size, p.width, p.height, p.format, p.sha256, p.quality, p.quality_json, "
            "       p.thumb, p.taken_at, p.camera, p.bad_flags, p.texture "
            "FROM group_members gm JOIN files f ON f.id=gm.file_id "
            "LEFT JOIN photos p ON p.file_id=gm.file_id "
            f"WHERE gm.group_id IN ({placeholders}) "
            "ORDER BY gm.group_id, gm.is_reference DESC, p.quality DESC",
            ids,
        )
        by_group: dict[int, list] = {}
        for row in member_rows:
            by_group.setdefault(int(row["group_id"]), []).append(row)

        groups: list[Group] = []
        for grow in group_rows:
            gid = int(grow["id"])
            members = []
            for row in by_group.get(gid, []):
                sig = PhotoSignature(
                    file_id=int(row["file_id"]),
                    path=row["path"],
                    size=int(row["size"] or 0),
                    width=int(row["width"] or 0),
                    height=int(row["height"] or 0),
                    format=row["format"] or "",
                    sha256=row["sha256"] or "",
                    quality=float(row["quality"] or 0.0),
                    texture=float(row["texture"] or 0.0),
                    taken_at=row["taken_at"] or "",
                    camera=row["camera"] or "",
                    thumb=row["thumb"] or "",
                    bad_flags=row["bad_flags"] or "",
                )
                members.append(
                    Member(
                        signature=sig,
                        similarity=float(row["similarity"]),
                        is_reference=bool(row["is_reference"]),
                        recommendation=row["recommendation"],
                        reason=row["reason"],
                        user_choice=row["user_choice"],
                        quality_report=QualityReport.from_dict(json.loads(row["quality_json"] or "{}")),
                        detail=json.loads(row["detail_json"] or "{}"),
                    )
                )
            if not members:
                continue
            groups.append(
                Group(
                    group_id=gid,
                    category=Category(grow["category"]),
                    members=members,
                    status=grow["status"],
                )
            )
        return groups

    def load_group(self, group_id: int) -> Group | None:
        """Carrega um grupo específico (usado ao atualizar um cartão na tela)."""
        rows = self.db.query("SELECT category FROM groups WHERE id=?", (group_id,))
        if not rows:
            return None
        for group in self.load_groups(categories=[rows[0]["category"]], order="id", limit=100000):
            if group.group_id == group_id:
                return group
        return None

    def count_groups(
        self,
        categories: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        filters: dict | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM groups g"
        where, params = [], []
        if categories:
            where.append(f"g.category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        if statuses:
            where.append(f"g.status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        member_sql, member_params = _member_filter_sql(filters)
        if member_sql:
            where.append(member_sql)
            params.extend(member_params)
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self.db.query_one(sql, params)
        return int(row["n"]) if row else 0

    def set_member_choice(self, group_id: int, file_id: int, choice: str | None) -> None:
        self.db.execute(
            "UPDATE group_members SET user_choice=? WHERE group_id=? AND file_id=?",
            (choice, group_id, file_id),
        )
        self.db.commit()

    def set_reference(self, group_id: int, file_id: int) -> None:
        """Troca a foto principal do grupo (a que será sempre mantida)."""
        with self.db.lock:
            conn = self.db.connection
            conn.execute("UPDATE group_members SET is_reference=0 WHERE group_id=?", (group_id,))
            conn.execute(
                "UPDATE group_members SET is_reference=1, user_choice='keep', recommendation='keep' "
                "WHERE group_id=? AND file_id=?",
                (group_id, file_id),
            )
            conn.execute("UPDATE groups SET reference_id=? WHERE id=?", (file_id, group_id))
            conn.commit()
        self.refresh_group_totals(group_id)

    def group_files(self, group_id: int) -> list[int]:
        return [int(r["file_id"]) for r in self.db.query("SELECT file_id FROM group_members WHERE group_id=?", (group_id,))]

    def set_group_status(self, group_id: int, status: str) -> None:
        self.db.execute("UPDATE groups SET status=? WHERE id=?", (status, group_id))
        self.db.commit()

    def refresh_group_totals(self, group_id: int) -> None:
        row = self.db.query_one(
            "SELECT SUM(CASE WHEN COALESCE(gm.user_choice, gm.recommendation)='remove' THEN f.size ELSE 0 END) AS wasted "
            "FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE gm.group_id=?",
            (group_id,),
        )
        self.db.execute("UPDATE groups SET wasted_bytes=? WHERE id=?", (int(row["wasted"] or 0), group_id))
        self.db.commit()

    def selected_for_removal(self) -> list[dict]:
        rows = self.db.query(
            "SELECT gm.group_id, gm.file_id, f.path, f.size, g.category, gm.similarity, p.quality "
            "FROM group_members gm "
            "JOIN files f ON f.id=gm.file_id "
            "JOIN groups g ON g.id=gm.group_id "
            "LEFT JOIN photos p ON p.file_id=gm.file_id "
            "WHERE COALESCE(gm.user_choice, gm.recommendation)='remove' AND gm.is_reference=0 "
            "AND f.status NOT IN ('removed','missing') "
            "ORDER BY gm.group_id"
        )
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- decisões
    def mark_not_duplicates(self, file_ids: Sequence[int]) -> int:
        """Registra que o usuário afirmou que estas fotos NÃO são duplicatas."""
        pairs = []
        ids = sorted({int(i) for i in file_ids})
        sha_by_id = {
            int(r["file_id"]): r["sha256"]
            for r in self.db.query(
                f"SELECT file_id, sha256 FROM photos WHERE file_id IN ({','.join('?' * len(ids))})", ids
            )
        } if ids else {}
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                pairs.append((a, b, sha_by_id.get(a, ""), sha_by_id.get(b, "")))
        if not pairs:
            return 0
        self.db.executemany(
            "INSERT INTO decisions(a, b, sha_a, sha_b, kind) VALUES(?,?,?,?,'not_duplicate') "
            "ON CONFLICT(a, b) DO NOTHING",
            pairs,
        )
        self.db.commit()
        return len(pairs)

    def excluded_pairs(self) -> set[tuple[int, int]]:
        return {
            (int(r["a"]), int(r["b"]))
            for r in self.db.query("SELECT a, b FROM decisions WHERE kind='not_duplicate'")
        }

    def clear_decisions(self) -> None:
        self.db.execute("DELETE FROM decisions")
        self.db.commit()

    # ------------------------------------------------------------------ ações
    def record_actions(self, batch: str, rows: Sequence[tuple[str, int | None, str, str, int, str, str]]) -> None:
        """Registra operações de arquivo para permitir desfazer."""
        self.db.executemany(
            "INSERT INTO actions(batch, action, file_id, src, dst, size, status, note) VALUES(?,?,?,?,?,?,?,?)",
            [(batch, *row) for row in rows],
        )
        self.db.commit()

    def list_batches(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT batch, MIN(ts) AS ts, COUNT(*) AS n, SUM(size) AS bytes, "
            "       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count, "
            "       MAX(action) AS action "
            "FROM actions GROUP BY batch ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def batch_actions(self, batch: str) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM actions WHERE batch=? ORDER BY id", (batch,))]

    def mark_action_undone(self, action_id: int) -> None:
        self.db.execute("UPDATE actions SET status='undone' WHERE id=?", (action_id,))
        self.db.commit()

    # ---------------------------------------------------------------- sessões
    def start_session(self, folders: Sequence[str]) -> int:
        cur = self.db.execute(
            "INSERT INTO sessions(folders_json, state) VALUES(?, 'running')",
            (json.dumps(list(folders), ensure_ascii=False),),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_session(self, session_id: int, state: str, stats: dict | None = None) -> None:
        self.db.execute(
            "UPDATE sessions SET state=?, stats_json=?, finished_at=CASE WHEN ? IN ('done','cancelled','error') "
            "THEN datetime('now') ELSE finished_at END WHERE id=?",
            (state, json.dumps(stats or {}, ensure_ascii=False), state, session_id),
        )
        self.db.commit()

    def last_session(self) -> dict | None:
        row = self.db.query_one("SELECT * FROM sessions ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    # ------------------------------------------------------------ estatísticas
    def summary(self) -> dict:
        def scalar(sql: str, params: tuple = ()) -> float:
            row = self.db.query_one(sql, params)
            value = row[0] if row else 0
            return float(value or 0)

        exact_groups = int(scalar("SELECT COUNT(*) FROM groups WHERE category='exact'"))
        visual_groups = int(scalar("SELECT COUNT(*) FROM groups WHERE category='visual'"))
        very_groups = int(scalar("SELECT COUNT(*) FROM groups WHERE category='very_similar'"))
        similar_groups = int(scalar("SELECT COUNT(*) FROM groups WHERE category='similar'"))
        return {
            "photos": int(scalar("SELECT COUNT(*) FROM photos")),
            "files": int(scalar("SELECT COUNT(*) FROM files")),
            "errors": int(scalar("SELECT COUNT(*) FROM files WHERE status='error'")),
            "total_bytes": int(scalar("SELECT SUM(size) FROM files WHERE status='analyzed'")),
            "exact_groups": exact_groups,
            "visual_groups": visual_groups,
            "very_similar_groups": very_groups,
            "similar_groups": similar_groups,
            "groups": exact_groups + visual_groups + very_groups + similar_groups,
            "exact_duplicates": int(
                scalar(
                    "SELECT COUNT(*) FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                    "WHERE g.category='exact' AND gm.is_reference=0"
                )
            ),
            "visual_duplicates": int(
                scalar(
                    "SELECT COUNT(*) FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                    "WHERE g.category='visual' AND gm.is_reference=0"
                )
            ),
            "reclaimable_bytes": int(scalar("SELECT SUM(wasted_bytes) FROM groups")),
            "redundant_bytes": int(
                scalar(
                    "SELECT SUM(f.size) FROM group_members gm JOIN files f ON f.id=gm.file_id "
                    "WHERE gm.is_reference=0 AND gm.group_id IN (SELECT id FROM groups WHERE category IN ('exact','visual'))"
                )
            ),
            "bad_photos": int(scalar("SELECT COUNT(*) FROM photos WHERE bad_flags<>''")),
            "blurry": int(scalar("SELECT COUNT(*) FROM photos WHERE bad_flags LIKE '%blurry%'")),
            "screenshots": int(scalar("SELECT COUNT(*) FROM photos WHERE bad_flags LIKE '%screenshot%'")),
            "no_exif": int(scalar("SELECT COUNT(*) FROM photos WHERE has_exif=0")),
        }

    def filtered_photos(self, filters: dict | None = None, order: str = "quality_asc", limit: int = 2000) -> list[dict]:
        """Consulta usada pelas telas de filtro e de fotos ruins."""
        filters = filters or {}
        where = ["f.status='analyzed'"]
        params: list = []
        if filters.get("bad_flag"):
            where.append("p.bad_flags LIKE ?")
            params.append(f"%{filters['bad_flag']}%")
        if filters.get("only_bad"):
            where.append("p.bad_flags <> ''")
        if filters.get("formats"):
            fmts = filters["formats"]
            where.append(f"UPPER(p.format) IN ({','.join('?' * len(fmts))})")
            params.extend([f.upper() for f in fmts])
        if filters.get("folder"):
            where.append("f.path LIKE ?")
            params.append(f"{filters['folder']}%")
        if filters.get("min_quality") is not None:
            where.append("p.quality >= ?")
            params.append(float(filters["min_quality"]))
        if filters.get("max_quality") is not None:
            where.append("p.quality <= ?")
            params.append(float(filters["max_quality"]))
        if filters.get("min_megapixels") is not None:
            where.append("(p.width * p.height) >= ?")
            params.append(float(filters["min_megapixels"]) * 1_000_000)
        if filters.get("max_megapixels") is not None:
            where.append("(p.width * p.height) <= ?")
            params.append(float(filters["max_megapixels"]) * 1_000_000)
        if filters.get("min_size") is not None:
            where.append("f.size >= ?")
            params.append(int(filters["min_size"]))
        if filters.get("has_exif") is True:
            where.append("p.has_exif=1")
        elif filters.get("has_exif") is False:
            where.append("p.has_exif=0")
        if filters.get("date_from"):
            where.append("p.taken_at >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            where.append("p.taken_at <= ?")
            params.append(filters["date_to"])
        if filters.get("text"):
            where.append("f.path LIKE ?")
            params.append(f"%{filters['text']}%")

        order_sql = {
            "quality_asc": "p.quality ASC",
            "quality_desc": "p.quality DESC",
            "size_desc": "f.size DESC",
            "size_asc": "f.size ASC",
            "resolution_desc": "(p.width * p.height) DESC",
            "resolution_asc": "(p.width * p.height) ASC",
            "date_desc": "p.taken_at DESC",
            "date_asc": "p.taken_at ASC",
            "folder": "f.path ASC",
        }.get(order, "p.quality ASC")

        sql = (
            "SELECT f.id AS file_id, f.path, f.size, p.width, p.height, p.format, p.quality, p.thumb, "
            "       p.bad_flags, p.taken_at, p.camera, p.has_exif, p.sharpness "
            "FROM photos p JOIN files f ON f.id=p.file_id WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order_sql} LIMIT {int(limit)}"
        )
        return [dict(r) for r in self.db.query(sql, params)]

    def distinct_formats(self) -> list[str]:
        return [r["format"] for r in self.db.query("SELECT DISTINCT UPPER(format) AS format FROM photos ORDER BY 1") if r["format"]]

    def distinct_folders(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT path FROM folders ORDER BY path")
        return [r["path"] for r in rows]

    def clear_results(self) -> None:
        """Limpa grupos e pares, mantendo o cache de análise das fotos."""
        with self.db.lock:
            conn = self.db.connection
            conn.execute("DELETE FROM group_members")
            conn.execute("DELETE FROM groups")
            conn.commit()

    def reset_all(self) -> None:
        with self.db.lock:
            conn = self.db.connection
            for table in ("group_members", "groups", "photos", "files", "decisions", "sessions"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()


def _member_filter_sql(filters: dict | None) -> tuple[str, list]:
    """Monta um EXISTS sobre os membros do grupo a partir dos filtros da interface."""
    filters = filters or {}
    conds: list[str] = []
    params: list = []
    if filters.get("text"):
        conds.append("f2.path LIKE ?")
        params.append(f"%{filters['text']}%")
    if filters.get("formats"):
        fmts = list(filters["formats"])
        conds.append(f"UPPER(p2.format) IN ({','.join('?' * len(fmts))})")
        params.extend([f.upper() for f in fmts])
    if filters.get("folder"):
        conds.append("f2.path LIKE ?")
        params.append(f"{filters['folder']}%")
    if filters.get("max_quality") is not None:
        conds.append("p2.quality <= ?")
        params.append(float(filters["max_quality"]))
    if filters.get("min_quality") is not None:
        conds.append("p2.quality >= ?")
        params.append(float(filters["min_quality"]))
    if filters.get("min_size") is not None:
        conds.append("f2.size >= ?")
        params.append(int(filters["min_size"]))
    if filters.get("min_megapixels") is not None:
        conds.append("(p2.width * p2.height) >= ?")
        params.append(float(filters["min_megapixels"]) * 1_000_000)
    if filters.get("max_megapixels") is not None:
        conds.append("(p2.width * p2.height) <= ?")
        params.append(float(filters["max_megapixels"]) * 1_000_000)
    if filters.get("has_exif") is True:
        conds.append("p2.has_exif=1")
    elif filters.get("has_exif") is False:
        conds.append("p2.has_exif=0")
    if filters.get("bad_flag"):
        conds.append("p2.bad_flags LIKE ?")
        params.append(f"%{filters['bad_flag']}%")
    if filters.get("date_from"):
        conds.append("p2.taken_at >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        conds.append("p2.taken_at <= ?")
        params.append(filters["date_to"])

    group_conds: list[str] = []
    if filters.get("min_similarity") is not None:
        group_conds.append("g.min_sim >= ?")
        params_tail = [float(filters["min_similarity"])]
    else:
        params_tail = []

    sql_parts: list[str] = []
    if conds:
        sql_parts.append(
            "EXISTS (SELECT 1 FROM group_members gm2 JOIN files f2 ON f2.id=gm2.file_id "
            "LEFT JOIN photos p2 ON p2.file_id=gm2.file_id "
            "WHERE gm2.group_id=g.id AND " + " AND ".join(conds) + ")"
        )
    sql_parts.extend(group_conds)
    params.extend(params_tail)
    return (" AND ".join(sql_parts), params) if sql_parts else ("", [])


def _exif_payload(exif: ExifData) -> dict:
    data = asdict(exif)
    raw = data.pop("raw", {}) or {}
    # Guardamos apenas um subconjunto do EXIF bruto, para o banco não inchar.
    data["raw"] = {k: raw[k] for k in list(raw)[:60]}
    return data
