"""Состояние обхода в SQLite: очередь URL, результаты, внешние встраивания.

Один процесс, несколько потоков: одно соединение под замком, WAL-журнал.
Переживает аварийное завершение: при старте незавершённые задачи
возвращаются в очередь. Каждая транзакция откатывается при ошибке,
чтобы соединение не «застревало» внутри BEGIN.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

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
    path_key      TEXT UNIQUE,      -- casefold(path) для ФС без учёта регистра
    sha1          TEXT,
    etag          TEXT,
    last_modified TEXT,
    location      TEXT,             -- цель редиректа (канонический URL)
    title         TEXT,
    tries         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    parent_id     INTEGER,
    discovered_at REAL,
    fetched_at    REAL,
    next_try_at   REAL NOT NULL DEFAULT 0   -- не раньше этого времени (отложенный повтор)
);
CREATE INDEX IF NOT EXISTS urls_queue ON urls(status, kind, priority, depth, id);
CREATE INDEX IF NOT EXISTS urls_status ON urls(status);
CREATE INDEX IF NOT EXISTS urls_parent ON urls(parent_id);

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

-- Каноническое написание папок: на NTFS/APFS «Abc» и «abc» — одна папка,
-- поэтому все файлы кладутся в папку с первым увиденным написанием.
CREATE TABLE IF NOT EXISTS dirs (
    dir_key TEXT PRIMARY KEY,
    dir     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_MIGRATIONS = [
    ("urls", "next_try_at", "ALTER TABLE urls ADD COLUMN next_try_at REAL NOT NULL DEFAULT 0"),
]


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


def path_key(path: str) -> str:
    return path.casefold()


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        # не давать файлу журнала разрастаться после контрольных точек
        self.conn.execute("PRAGMA journal_size_limit=67108864")
        self._migrate_before_schema()
        self.conn.executescript(SCHEMA)
        self._dir_cache: dict[str, str] = {}

    def _migrate_before_schema(self) -> None:
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, col, sql in _MIGRATIONS:
            if table in tables:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self.conn.execute(sql)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция под замком; при любой ошибке — ROLLBACK."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def checkpoint(self) -> None:
        """Перенести журнал в базу и усечь его (вызывается периодически)."""
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

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

    def del_meta(self, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))

    # -- очередь ------------------------------------------------------------
    def add_urls(self, rows: Iterable[tuple[str, str | None, str, int, int, int | None]]) -> int:
        """rows: (url, alt_url, kind, priority, depth, parent_id). Возвращает число новых."""
        rows = list(rows)
        if not rows:
            return 0
        now = time.time()
        with self.tx() as c:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO urls(url, alt_url, kind, priority, depth, parent_id, discovered_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [(u, alt, k, p, d, par, now) for (u, alt, k, p, d, par) in rows],
            )
            inserted = c.total_changes - before
            # повысить приоритет уже известных, если нашли более важный путь к ним
            c.executemany(
                "UPDATE urls SET priority=? WHERE url=? AND status='queued' AND priority>?",
                [(p, u, p) for (u, _alt, _k, p, _d, _par) in rows],
            )
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

    def add_skips(self, rows: list[tuple[str, str, int, str]]) -> None:
        """rows: (reason, prefix, n, example) — накопленные счётчики пропусков."""
        if not rows:
            return
        with self.tx() as c:
            c.executemany(
                "INSERT INTO skipped_stats(reason, prefix, n, example) VALUES(?,?,?,?)"
                " ON CONFLICT(reason, prefix) DO UPDATE SET n=n+excluded.n",
                rows,
            )

    def claim(self, kinds: tuple[str, ...], limit: int) -> list[Task]:
        with self.lock:
            q = (
                "SELECT id, url, alt_url, kind, priority, depth, tries, path, etag, last_modified FROM urls"
                " WHERE status='queued' AND kind IN (%s) AND next_try_at <= ?"
                " ORDER BY priority, depth, id LIMIT ?"
            ) % ",".join("?" * len(kinds))
            rows = self.conn.execute(q, (*kinds, time.time(), limit)).fetchall()
            if not rows:
                return []
            with self.tx() as c:
                c.executemany("UPDATE urls SET status='active' WHERE id=?", [(r["id"],) for r in rows])
        return [Task(**dict(r)) for r in rows]

    def next_due_in(self) -> float | None:
        """Через сколько секунд станет доступна ближайшая отложенная задача."""
        with self.lock:
            row = self.conn.execute(
                "SELECT MIN(next_try_at) FROM urls WHERE status='queued'"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return max(0.0, row[0] - time.time())

    # -- пути файлов ----------------------------------------------------------
    def _canonical_dirs(self, relpath: str) -> str:
        """Привести написание папок к уже существующему (без учёта регистра)."""
        parts = relpath.split("/")
        if len(parts) <= 1:
            return relpath
        out: list[str] = []
        for i, seg in enumerate(parts[:-1]):
            prefix = "/".join(out + [seg])
            key = path_key(prefix)
            canon = self._dir_cache.get(key)
            if canon is None:
                row = self.conn.execute("SELECT dir FROM dirs WHERE dir_key=?", (key,)).fetchone()
                if row is None:
                    self.conn.execute("INSERT OR IGNORE INTO dirs(dir_key, dir) VALUES(?,?)", (key, prefix))
                    canon = prefix
                else:
                    canon = row[0]
                if len(self._dir_cache) > 200_000:
                    self._dir_cache.clear()
                self._dir_cache[key] = canon
            out = canon.split("/")
        return "/".join(out + [parts[-1]])

    def path_owner(self, path: str) -> int | None:
        with self.lock:
            row = self.conn.execute("SELECT id FROM urls WHERE path_key=?", (path_key(path),)).fetchone()
        return row[0] if row else None

    def reserve_path(self, task_id: int, relpath: str, salt: str) -> str:
        """Закрепляет за задачей уникальный (без учёта регистра) путь файла."""
        from .urls import disambiguate

        with self.lock:
            relpath = self._canonical_dirs(relpath)
            cand = relpath
            for i in range(20):
                row = self.conn.execute("SELECT id FROM urls WHERE path_key=?", (path_key(cand),)).fetchone()
                if row is None or row[0] == task_id:
                    self.conn.execute("UPDATE urls SET path=?, path_key=? WHERE id=?", (cand, path_key(cand), task_id))
                    return cand
                cand = disambiguate(relpath, f"{salt}#{i}")
        raise RuntimeError(f"не удалось подобрать уникальный путь для {relpath}")

    def get_by_url(self, url: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()

    # -- результаты -----------------------------------------------------------
    def finish(self, task_id: int, **fields) -> None:
        fields.setdefault("fetched_at", time.time())
        if "path" in fields:
            fields["path_key"] = path_key(fields["path"]) if fields["path"] else None
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE urls SET {cols} WHERE id=?", (*fields.values(), task_id))

    def requeue(self, task_id: int, error: str, tries: int, delay: float = 0.0) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE urls SET status='queued', error=?, tries=?, next_try_at=? WHERE id=?",
                (error, tries, time.time() + delay if delay > 0 else 0, task_id),
            )

    def requeue_where(self, where: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(
                f"UPDATE urls SET status='queued', tries=0, next_try_at=0 WHERE {where}", params
            )
            return cur.rowcount

    # -- встраивания ---------------------------------------------------------
    def add_embeds(self, rows: Iterable[tuple[str, str]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO embeds(url, page_url) VALUES(?,?)", rows)

    # -- чтение больших выборок ------------------------------------------------
    def iter_chunks(self, cols: str, where: str = "1", params: tuple = (), start_id: int = 0,
                    chunk: int = 2000) -> Iterator[sqlite3.Row]:
        """Обход большой выборки короткими порциями по id.

        Длинный открытый курсор мешал бы WAL-журналу сбрасываться, и он рос бы
        на гигабайты. Здесь каждая порция читается целиком и сразу отпускается.
        Первая колонка ``cols`` должна быть id.
        """
        last = start_id
        while True:
            with self.lock:
                rows = self.conn.execute(
                    f"SELECT {cols} FROM urls WHERE ({where}) AND id > ? ORDER BY id LIMIT ?",
                    (*params, last, chunk),
                ).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1][0]

    # -- отчёты ----------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")}

    def total_bytes(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COALESCE(SUM(size),0) FROM urls WHERE status='done'").fetchone()[0]

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).rowcount
