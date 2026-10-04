"""Состояние обхода в SQLite: очередь URL, результаты, внешние встраивания.

Один процесс, несколько потоков: одно соединение под замком, WAL-журнал.
Переживает аварийное завершение: при старте незавершённые задачи
возвращаются в очередь.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    alt_url       TEXT,             -- исходный URL до замены хоста-алиаса
    kind          TEXT NOT NULL,    -- page | asset | media | sitemap
    priority      INTEGER NOT NULL DEFAULT 50,
    depth         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'queued',
        -- queued | active | done | redirect | notfound | error | skipped | auth
    http_status   INTEGER,
    content_type  TEXT,
    size          INTEGER,
    path          TEXT,             -- относительный путь в mirror/
    path_key      TEXT UNIQUE,      -- lower(path) для ФС без учёта регистра
    sha1          TEXT,
    etag          TEXT,
    last_modified TEXT,
    location      TEXT,             -- цель редиректа (канонический URL)
    title         TEXT,
    tries         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    parent_id     INTEGER,
    discovered_at REAL,
    fetched_at    REAL
);
CREATE INDEX IF NOT EXISTS urls_queue ON urls(status, kind, priority, depth, id);
CREATE INDEX IF NOT EXISTS urls_status ON urls(status);

CREATE TABLE IF NOT EXISTS query_counts (
    host_path TEXT PRIMARY KEY,
    n         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS embeds (
    url       TEXT PRIMARY KEY,   -- внешний плеер/видео (YouTube, RuTube, VK ...)
    page_url  TEXT,
    status    TEXT NOT NULL DEFAULT 'new',  -- new | done | error
    path      TEXT,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS skipped_stats (
    reason  TEXT NOT NULL,
    prefix  TEXT NOT NULL,
    n       INTEGER NOT NULL,
    example TEXT,
    PRIMARY KEY (reason, prefix)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Task:
    id: int
    url: str
    alt_url: str | None
    kind: str
    priority: int
    depth: int
    tries: int
    path: str | None
    etag: str | None
    last_modified: str | None


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- служебное --------------------------------------------------------
    def reset_active(self) -> int:
        with self.lock:
            cur = self.conn.execute("UPDATE urls SET status='queued' WHERE status='active'")
            return cur.rowcount

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    # -- очередь ------------------------------------------------------------
    def add_urls(self, rows: Iterable[tuple[str, str | None, str, int, int, int | None]]) -> int:
        """rows: (url, alt_url, kind, priority, depth, parent_id). Возвращает число новых."""
        rows = list(rows)
        if not rows:
            return 0
        now = time.time()
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                before = self.conn.total_changes
                self.conn.executemany(
                    "INSERT OR IGNORE INTO urls(url, alt_url, kind, priority, depth, parent_id, discovered_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    [(u, alt, k, p, d, par, now) for (u, alt, k, p, d, par) in rows],
                )
                inserted = self.conn.total_changes - before
                # повысить приоритет уже известных, если нашли более важный путь к ним
                self.conn.executemany(
                    "UPDATE urls SET priority=? WHERE url=? AND status='queued' AND priority>?",
                    [(p, u, p) for (u, _alt, _k, p, _d, _par) in rows],
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return inserted

    def known(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        out: set[str] = set()
        with self.lock:
            for i in range(0, len(urls), 500):
                chunk = urls[i : i + 500]
                q = "SELECT url FROM urls WHERE url IN (%s)" % ",".join("?" * len(chunk))
                out.update(r[0] for r in self.conn.execute(q, chunk))
        return out

    def bump_query_count(self, host_path: str, cap: int) -> bool:
        """Учитывает ещё один вариант query для пути. False — лимит исчерпан."""
        with self.lock:
            row = self.conn.execute("SELECT n FROM query_counts WHERE host_path=?", (host_path,)).fetchone()
            n = row[0] if row else 0
            if n >= cap:
                return False
            self.conn.execute("INSERT OR REPLACE INTO query_counts(host_path, n) VALUES(?,?)", (host_path, n + 1))
            return True

    def count_skip(self, reason: str, prefix: str, example: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skipped_stats(reason, prefix, n, example) VALUES(?,?,1,?)"
                " ON CONFLICT(reason, prefix) DO UPDATE SET n=n+1",
                (reason, prefix, example),
            )

    def claim(self, kinds: tuple[str, ...], limit: int) -> list[Task]:
        with self.lock:
            q = (
                "SELECT id, url, alt_url, kind, priority, depth, tries, path, etag, last_modified FROM urls"
                " WHERE status='queued' AND kind IN (%s) ORDER BY priority, depth, id LIMIT ?"
            ) % ",".join("?" * len(kinds))
            rows = self.conn.execute(q, (*kinds, limit)).fetchall()
            if not rows:
                return []
            self.conn.execute("BEGIN")
            self.conn.executemany("UPDATE urls SET status='active' WHERE id=?", [(r["id"],) for r in rows])
            self.conn.execute("COMMIT")
        return [Task(**dict(r)) for r in rows]

    def path_owner(self, path: str) -> int | None:
        with self.lock:
            row = self.conn.execute("SELECT id FROM urls WHERE path_key=?", (path.lower(),)).fetchone()
        return row[0] if row else None

    def reserve_path(self, task_id: int, relpath: str, salt: str) -> str:
        """Закрепляет за задачей уникальный (без учёта регистра) путь файла."""
        from .urls import disambiguate

        with self.lock:
            cand = relpath
            for i in range(20):
                row = self.conn.execute("SELECT id FROM urls WHERE path_key=?", (cand.lower(),)).fetchone()
                if row is None or row[0] == task_id:
                    self.conn.execute("UPDATE urls SET path=?, path_key=? WHERE id=?", (cand, cand.lower(), task_id))
                    return cand
                cand = disambiguate(relpath, f"{salt}#{i}")
        raise RuntimeError(f"не удалось подобрать уникальный путь для {relpath}")

    def get_by_url(self, url: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()

    def finish(self, task_id: int, **fields) -> None:
        fields.setdefault("fetched_at", time.time())
        if "path" in fields:
            fields["path_key"] = fields["path"].lower() if fields["path"] else None
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE urls SET {cols} WHERE id=?", (*fields.values(), task_id))

    def requeue(self, task_id: int, error: str, tries: int) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE urls SET status='queued', error=?, tries=? WHERE id=?", (error, tries, task_id)
            )

    def requeue_where(self, where: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(f"UPDATE urls SET status='queued', tries=0 WHERE {where}", params)
            return cur.rowcount

    # -- встраивания ---------------------------------------------------------
    def add_embeds(self, rows: Iterable[tuple[str, str]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO embeds(url, page_url) VALUES(?,?)", rows)

    # -- отчёты ----------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")}

    def counts_by(self, expr: str) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                f"SELECT {expr} AS g, status, kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
                " FROM urls GROUP BY g, status, kind ORDER BY g"
            ).fetchall()

    def total_bytes(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COALESCE(SUM(size),0) FROM urls WHERE status='done'").fetchone()[0]

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).rowcount
