# -*- coding: utf-8 -*-
"""SQLite 持久化存储模块：管理项目白名单、Release 元数据缓存与本地文件索引。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class RelayStorage:
    """线程安全的 SQLite 存储封装。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS repos (
                    repo TEXT PRIMARY KEY COLLATE NOCASE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS release_cache (
                    repo TEXT NOT NULL COLLATE NOCASE,
                    tag TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (repo, tag)
                );

                CREATE TABLE IF NOT EXISTS cached_files (
                    repo TEXT NOT NULL COLLATE NOCASE,
                    tag TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    PRIMARY KEY (repo, tag, filename)
                );
                """
            )

    # ---- 白名单仓库管理 ----

    def list_repos(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT repo, enabled, note, created_at, updated_at FROM repos ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_repo(self, repo: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT repo, enabled, note, created_at, updated_at FROM repos WHERE repo = ? COLLATE NOCASE",
                (repo,),
            ).fetchone()
            return dict(row) if row else None

    def is_repo_enabled(self, repo: str) -> bool:
        item = self.get_repo(repo)
        return bool(item and item["enabled"])

    def upsert_repo(self, repo: str, enabled: bool = True, note: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT repo FROM repos WHERE repo = ? COLLATE NOCASE", (repo,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE repos SET enabled = ?, note = ?, updated_at = ? WHERE repo = ? COLLATE NOCASE",
                    (1 if enabled else 0, note.strip(), now, repo),
                )
            else:
                conn.execute(
                    "INSERT INTO repos (repo, enabled, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (repo, 1 if enabled else 0, note.strip(), now, now),
                )
        result = self.get_repo(repo)
        assert result is not None
        return result

    def set_repo_enabled(self, repo: str, enabled: bool) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE repos SET enabled = ?, updated_at = ? WHERE repo = ? COLLATE NOCASE",
                (1 if enabled else 0, now, repo),
            )
            return cur.rowcount > 0

    def delete_repo(self, repo: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM release_cache WHERE repo = ? COLLATE NOCASE", (repo,))
            conn.execute("DELETE FROM cached_files WHERE repo = ? COLLATE NOCASE", (repo,))
            cur = conn.execute("DELETE FROM repos WHERE repo = ? COLLATE NOCASE", (repo,))
            return cur.rowcount > 0

    # ---- Release 元数据缓存 ----

    def get_release_cache(self, repo: str, tag: str = "__latest__") -> tuple[dict[str, Any], float] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at FROM release_cache WHERE repo = ? COLLATE NOCASE AND tag = ?",
                (repo, tag),
            ).fetchone()
            if not row:
                return None
            return json.loads(row["payload_json"]), float(row["fetched_at"])

    def set_release_cache(self, repo: str, tag: str, payload: dict[str, Any]) -> None:
        now = time.time()
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO release_cache (repo, tag, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo, tag) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (repo, tag, serialized, now),
            )

    def delete_release_cache(self, repo: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM release_cache WHERE repo = ? COLLATE NOCASE", (repo,))

    # ---- 缓存文件索引 ----

    def get_cached_file(self, repo: str, tag: str, filename: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, tag, filename, rel_path, size, content_type, created_at, last_accessed_at
                FROM cached_files
                WHERE repo = ? COLLATE NOCASE AND tag = ? AND filename = ?
                """,
                (repo, tag, filename),
            ).fetchone()
            return dict(row) if row else None

    def upsert_cached_file(
        self,
        repo: str,
        tag: str,
        filename: str,
        rel_path: str,
        size: int,
        content_type: str,
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cached_files (repo, tag, filename, rel_path, size, content_type, created_at, last_accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, tag, filename) DO UPDATE SET
                    rel_path = excluded.rel_path,
                    size = excluded.size,
                    content_type = excluded.content_type,
                    created_at = excluded.created_at,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (repo, tag, filename, rel_path, size, content_type, now, now),
            )

    def touch_cached_file(self, repo: str, tag: str, filename: str) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE cached_files SET last_accessed_at = ?
                WHERE repo = ? COLLATE NOCASE AND tag = ? AND filename = ?
                """,
                (now, repo, tag, filename),
            )

    def list_cached_files(self, repo: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if repo:
                rows = conn.execute(
                    """
                    SELECT repo, tag, filename, rel_path, size, content_type, created_at, last_accessed_at
                    FROM cached_files WHERE repo = ? COLLATE NOCASE
                    ORDER BY last_accessed_at DESC
                    """,
                    (repo,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT repo, tag, filename, rel_path, size, content_type, created_at, last_accessed_at
                    FROM cached_files
                    ORDER BY last_accessed_at ASC
                    """
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_cached_file(self, repo: str, tag: str, filename: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, tag, filename, rel_path, size
                FROM cached_files
                WHERE repo = ? COLLATE NOCASE AND tag = ? AND filename = ?
                """,
                (repo, tag, filename),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                DELETE FROM cached_files
                WHERE repo = ? COLLATE NOCASE AND tag = ? AND filename = ?
                """,
                (repo, tag, filename),
            )
            return dict(row)

    def delete_repo_cached_files(self, repo: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT repo, tag, filename, rel_path, size FROM cached_files WHERE repo = ? COLLATE NOCASE",
                (repo,),
            ).fetchall()
            conn.execute("DELETE FROM cached_files WHERE repo = ? COLLATE NOCASE", (repo,))
            return [dict(r) for r in rows]
