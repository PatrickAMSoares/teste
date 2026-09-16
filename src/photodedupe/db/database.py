"""Banco de dados local em SQLite.

Decisões de projeto:

* **WAL** (*write-ahead logging*): permite que a interface leia enquanto a
  análise grava, sem travar a tela.
* Tudo em um único arquivo dentro do perfil do usuário - nada de servidor,
  nada de rede. O aplicativo funciona totalmente offline.
* Migrações versionadas: bancos de versões anteriores são atualizados na
  abertura, preservando o cache de análise já existente.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from ..paths import db_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS folders (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    enabled   INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    folder_id     INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    mtime_ns      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    analyzed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(ext);

CREATE TABLE IF NOT EXISTS photos (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0,
    format       TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL DEFAULT '',
    phash        INTEGER NOT NULL DEFAULT 0,
    dhash        INTEGER NOT NULL DEFAULT 0,
    ahash        INTEGER NOT NULL DEFAULT 0,
    whash        INTEGER NOT NULL DEFAULT 0,
    color_sig    INTEGER NOT NULL DEFAULT 0,
    crop_phash   INTEGER NOT NULL DEFAULT 0,
    crop_dhash   INTEGER NOT NULL DEFAULT 0,
    gray         BLOB,
    descriptor   BLOB,
    crop_desc    BLOB,
    desc_version INTEGER NOT NULL DEFAULT 0,
    quality      REAL NOT NULL DEFAULT 0,
    quality_json TEXT NOT NULL DEFAULT '{}',
    sharpness    REAL NOT NULL DEFAULT 0,
    bpp          REAL NOT NULL DEFAULT 0,
    texture      REAL NOT NULL DEFAULT 0,
    exif_json    TEXT NOT NULL DEFAULT '{}',
    has_exif     INTEGER NOT NULL DEFAULT 0,
    taken_at     TEXT NOT NULL DEFAULT '',
    capture_key  TEXT NOT NULL DEFAULT '',
    camera       TEXT NOT NULL DEFAULT '',
    lens         TEXT NOT NULL DEFAULT '',
    iso          INTEGER,
    aperture     REAL,
    shutter      TEXT NOT NULL DEFAULT '',
    gps_lat      REAL,
    gps_lon      REAL,
    bad_flags    TEXT NOT NULL DEFAULT '',
    bad_json     TEXT NOT NULL DEFAULT '{}',
    thumb        TEXT NOT NULL DEFAULT '',
    analyzed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photos_sha     ON photos(sha256);
CREATE INDEX IF NOT EXISTS idx_photos_quality ON photos(quality);
CREATE INDEX IF NOT EXISTS idx_photos_bad     ON photos(bad_flags);
CREATE INDEX IF NOT EXISTS idx_photos_taken   ON photos(taken_at);

CREATE TABLE IF NOT EXISTS groups (
    id           INTEGER PRIMARY KEY,
    category     TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    total_bytes  INTEGER NOT NULL DEFAULT 0,
    wasted_bytes INTEGER NOT NULL DEFAULT 0,
    min_sim      REAL NOT NULL DEFAULT 0,
    reference_id INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_groups_category ON groups(category);
CREATE INDEX IF NOT EXISTS idx_groups_status   ON groups(status);

CREATE TABLE IF NOT EXISTS group_members (
    group_id       INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    similarity     REAL NOT NULL DEFAULT 0,
    is_reference   INTEGER NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT 'keep',
    reason         TEXT NOT NULL DEFAULT '',
    user_choice    TEXT,
    detail_json    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_gm_file ON group_members(file_id);

CREATE TABLE IF NOT EXISTS decisions (
    a          INTEGER NOT NULL,
    b          INTEGER NOT NULL,
    sha_a      TEXT NOT NULL DEFAULT '',
    sha_b      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'not_duplicate',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS actions (
    id       INTEGER PRIMARY KEY,
    batch    TEXT NOT NULL,
    ts       TEXT NOT NULL DEFAULT (datetime('now')),
    action   TEXT NOT NULL,
    file_id  INTEGER,
    src      TEXT NOT NULL DEFAULT '',
    dst      TEXT NOT NULL DEFAULT '',
    size     INTEGER NOT NULL DEFAULT 0,
    status   TEXT NOT NULL DEFAULT 'ok',
    note     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_actions_batch ON actions(batch);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    state        TEXT NOT NULL DEFAULT 'running',
    folders_json TEXT NOT NULL DEFAULT '[]',
    stats_json   TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    """Conexão SQLite protegida por lock (usada por interface e análise)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    # ------------------------------------------------------------------ setup
    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-65536")     # ~64 MB de cache de páginas
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            current = int(self.get_meta("schema_version", "0") or 0)
            if current and current < SCHEMA_VERSION:
                self._apply_migrations(current)
            if current != SCHEMA_VERSION:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
                log.info("Banco no esquema v%s", SCHEMA_VERSION)
            self._conn.commit()

    def _apply_migrations(self, current: int) -> None:
        """Atualiza bancos de versões anteriores preservando o cache de análise."""
        if current < 2:
            # v2: assinaturas do recorte central (detecção de fotos recortadas).
            for column, ddl in (
                ("crop_phash", "INTEGER NOT NULL DEFAULT 0"),
                ("crop_dhash", "INTEGER NOT NULL DEFAULT 0"),
                ("crop_desc", "BLOB"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE photos ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError:
                    pass  # coluna já existe
            # Força a reanálise para preencher as novas assinaturas.
            self._conn.execute("UPDATE files SET status='pending' WHERE status='analyzed'")
            log.info("Migração v2 aplicada: as fotos serão reanalisadas para gerar as assinaturas de recorte")

    # ------------------------------------------------------------------- infra
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, seq)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows

    def query_one(self, sql: str, params: tuple | list = ()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    # -------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------- manutenção
    def vacuum(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM")

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("files", "photos", "groups", "group_members", "decisions", "actions"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        return out
