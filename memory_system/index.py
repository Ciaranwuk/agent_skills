from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import discover_markdown_files, normalize_source_roots

SOURCE = "memory"
SCHEMA_VERSION = 1
SOURCE_ALLOWLIST_VERSION = 1
TOKENIZER = "unicode61 remove_diacritics 2"
META_KEY = "memory_index_meta_v1"
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_SYNC_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class FileEntry:
    path: str
    abs_path: Path
    mtime: int
    size: int
    file_hash: str


class MemoryIndex:
    def __init__(
        self,
        workspace: str | Path,
        db_path: str | Path | None = None,
        chunk_tokens: int = 180,
        chunk_overlap: int = 40,
        source_roots: list[str | Path] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(db_path).resolve() if db_path else self.workspace / ".memory_index.sqlite"
        self.chunk_tokens = chunk_tokens
        self.chunk_overlap = chunk_overlap
        self.source_roots = normalize_source_roots(self.workspace, source_roots)

    def sync(self, force: bool = False) -> dict[str, int]:
        sync_lock = _get_sync_lock(self.db_path)
        if not sync_lock.acquire(blocking=False):
            return {"indexed": 0, "removed": 0, "unchanged": 0, "in_progress": 1}

        try:
            manifest = self._build_manifest()
            if force or not self.db_path.exists() or self._requires_rebuild():
                indexed = self._full_rebuild(manifest)
                return {"indexed": indexed, "removed": 0, "unchanged": 0}

            max_attempts = 3
            for attempt in range(max_attempts):
                conn = self._connect(self.db_path)
                try:
                    self._init_schema(conn)
                    conn.execute("BEGIN")
                    changed = 0
                    unchanged = 0
                    removed = 0
                    stale = self._get_stale_paths(conn, set(manifest.keys()))

                    for path, entry in manifest.items():
                        row = conn.execute("SELECT hash FROM files WHERE path = ? AND source = ?", (path, SOURCE)).fetchone()
                        if row and row[0] == entry.file_hash:
                            unchanged += 1
                            continue
                        try:
                            self._replace_path_content(conn, entry)
                            changed += 1
                        except FileNotFoundError:
                            # File vanished after manifest scan; treat as stale/removed.
                            self._delete_path_rows(conn, path)
                            removed += 1

                    for stale_path in stale:
                        self._delete_path_rows(conn, stale_path)
                        removed += 1

                    conn.commit()
                    return {"indexed": changed, "removed": removed, "unchanged": unchanged}
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    text = str(exc).lower()
                    if ("locked" in text or "busy" in text) and attempt < (max_attempts - 1):
                        time.sleep(0.1 * (2**attempt))
                        continue
                    raise
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

            raise RuntimeError("sync retry attempts exhausted")
        finally:
            sync_lock.release()

    def search(self, query: str, max_results: int = 10, min_score: float = 0.0) -> list[dict[str, Any]]:
        conn = self._connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT c.path, c.start_line, c.end_line, c.text, c.id, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.id
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC, c.path ASC, c.start_line ASC, c.id ASC
                LIMIT ?
                """,
                (query, int(max_results)),
            ).fetchall()
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for path, start_line, end_line, text, chunk_id, rank in rows:
            score = round(1.0 / (1.0 + abs(float(rank))), 6)
            if score < min_score:
                continue
            snippet = " ".join((text or "").split())[:240]
            results.append(
                {
                    "path": path,
                    "startLine": int(start_line),
                    "endLine": int(end_line),
                    "score": score,
                    "snippet": snippet,
                    "source": SOURCE,
                    "_id": chunk_id,
                }
            )

        results.sort(key=lambda r: (-r["score"], r["path"], r["startLine"], r["_id"]))
        for row in results:
            del row["_id"]
        return results

    def is_available(self) -> tuple[bool, str | None]:
        if not self.db_path.exists():
            return False, "index file not found"
        try:
            conn = self._connect(self.db_path)
            try:
                required = {"meta", "files", "chunks", "chunks_fts"}
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
                present = {r[0] for r in rows}
                if not required.issubset(present):
                    return False, "required index tables missing"
                meta = self._read_meta(conn)
                if meta is None:
                    return False, "index metadata missing"
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return False, f"sqlite error: {exc}"
        return True, None

    def _requires_rebuild(self) -> bool:
        try:
            conn = self._connect(self.db_path)
            try:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
                present = {r[0] for r in rows}
                required = {"meta", "files", "chunks", "chunks_fts"}
                if not required.issubset(present):
                    return True
                meta = self._read_meta(conn)
                expected = self._meta_payload()
                return meta != expected
            finally:
                conn.close()
        except sqlite3.Error:
            return True

    def _build_manifest(self) -> dict[str, FileEntry]:
        out: dict[str, FileEntry] = {}
        for item in discover_markdown_files(self.source_roots):
            try:
                stat = item.abs_path.stat()
                file_hash = self._hash_file(item.abs_path)
            except FileNotFoundError:
                continue
            out[item.key] = FileEntry(
                path=item.key,
                abs_path=item.abs_path,
                mtime=int(stat.st_mtime_ns),
                size=int(stat.st_size),
                file_hash=file_hash,
            )
        return out

    def _full_rebuild(self, manifest: dict[str, FileEntry]) -> int:
        tmp_path = self.db_path.with_name(f"{self.db_path.name}.tmp-{uuid.uuid4().hex}")
        if tmp_path.exists():
            tmp_path.unlink()

        conn = self._connect(tmp_path)
        indexed = 0
        try:
            self._init_schema(conn)
            conn.execute("BEGIN")
            for entry in manifest.values():
                try:
                    self._replace_path_content(conn, entry, prune_existing=False)
                    indexed += 1
                except FileNotFoundError:
                    continue
            self._write_meta(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, self.db_path)
        self._cleanup_sidecars(tmp_path)
        self._cleanup_sidecars(self.db_path)
        return indexed

    def _replace_path_content(self, conn: sqlite3.Connection, entry: FileEntry, prune_existing: bool = True) -> None:
        data = entry.abs_path.read_bytes()
        text = data.decode("utf-8")
        chunks = _chunk_text(text, self.chunk_tokens, self.chunk_overlap)

        if prune_existing:
            self._delete_path_rows(conn, entry.path)

        now = int(time.time())
        for chunk in chunks:
            chunk_hash = _sha256_hex(chunk["text"].encode("utf-8"))
            chunk_id = _sha256_hex(
                f"{SOURCE}:{entry.path}:{chunk['start_line']}:{chunk['end_line']}:{chunk_hash}".encode("utf-8")
            )
            conn.execute(
                """
                INSERT INTO chunks (id, path, source, start_line, end_line, hash, text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    entry.path,
                    SOURCE,
                    chunk["start_line"],
                    chunk["end_line"],
                    chunk_hash,
                    chunk["text"],
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO chunks_fts (text, id, path, source, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chunk["text"], chunk_id, entry.path, SOURCE, chunk["start_line"], chunk["end_line"]),
            )

        conn.execute(
            """
            INSERT INTO files (path, source, hash, mtime, size, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                source=excluded.source,
                hash=excluded.hash,
                mtime=excluded.mtime,
                size=excluded.size,
                indexed_at=excluded.indexed_at
            """,
            (entry.path, SOURCE, entry.file_hash, entry.mtime, entry.size, now),
        )

    def _delete_path_rows(self, conn: sqlite3.Connection, rel_path: str) -> None:
        conn.execute("DELETE FROM chunks_fts WHERE path = ? AND source = ?", (rel_path, SOURCE))
        conn.execute("DELETE FROM chunks WHERE path = ? AND source = ?", (rel_path, SOURCE))
        conn.execute("DELETE FROM files WHERE path = ? AND source = ?", (rel_path, SOURCE))

    def _get_stale_paths(self, conn: sqlite3.Connection, active_paths: set[str]) -> list[str]:
        rows = conn.execute("SELECT path FROM files WHERE source = ?", (SOURCE,)).fetchall()
        stale = sorted({row[0] for row in rows} - active_paths)
        return stale

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY,
              source TEXT NOT NULL DEFAULT 'memory',
              hash TEXT NOT NULL,
              mtime INTEGER NOT NULL,
              size INTEGER NOT NULL,
              indexed_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_files_source ON files(source);
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);

            CREATE TABLE IF NOT EXISTS chunks (
              id TEXT PRIMARY KEY,
              path TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'memory',
              start_line INTEGER NOT NULL,
              end_line INTEGER NOT NULL,
              hash TEXT NOT NULL,
              text TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
              CHECK (start_line >= 1),
              CHECK (end_line >= start_line)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
            CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks(source, path);
            CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(hash);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
              text,
              id UNINDEXED,
              path UNINDEXED,
              source UNINDEXED,
              start_line UNINDEXED,
              end_line UNINDEXED,
              tokenize = 'unicode61 remove_diacritics 2'
            );
            """
        )

    def _write_meta(self, conn: sqlite3.Connection) -> None:
        payload = json.dumps(self._meta_payload(), sort_keys=True)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_KEY, payload),
        )

    def _read_meta(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def _meta_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "chunkTokens": self.chunk_tokens,
            "chunkOverlap": self.chunk_overlap,
            "sourceAllowlistVersion": SOURCE_ALLOWLIST_VERSION,
            "tokenizer": TOKENIZER,
            "sourceRootsFingerprint": self._source_roots_fingerprint(),
        }

    @staticmethod
    def _hash_file(path: Path) -> str:
        return _sha256_hex(path.read_bytes())

    def _source_roots_fingerprint(self) -> str:
        payload = "|".join(f"{root.alias}={root.path.as_posix()}" for root in self.source_roots)
        return _sha256_hex(payload.encode("utf-8"))

    @staticmethod
    def _cleanup_sidecars(base_path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{base_path}{suffix}")
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError:
                    pass


def _chunk_text(text: str, chunk_tokens: int, chunk_overlap: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []

    token_counts = [max(1, len(line.split())) for line in lines]
    chunks: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    while i < n:
        start = i
        total = 0
        end = i
        while end < n and (total < chunk_tokens or end == start):
            total += token_counts[end]
            end += 1

        chunk_lines = lines[start:end]
        chunks.append(
            {
                "start_line": start + 1,
                "end_line": end,
                "text": "\n".join(chunk_lines),
            }
        )

        if end >= n:
            break

        back = 0
        next_start = end
        while next_start > start and back < chunk_overlap:
            next_start -= 1
            back += token_counts[next_start]

        if next_start <= start:
            i = end
        else:
            i = next_start

    return chunks


def _sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _get_sync_lock(db_path: Path) -> threading.Lock:
    key = str(db_path.resolve())
    with _SYNC_LOCKS_GUARD:
        lock = _SYNC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SYNC_LOCKS[key] = lock
        return lock
