"""Полнотекстовый поиск по архиву (SQLite FTS5, без внешних зависимостей)."""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import time
from html.parser import HTMLParser

from .extract import decode_body
from .fsutil import mirror_file

log = logging.getLogger("azbyka_reserv")

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "object", "button", "select", "form"}
_BOILER_RX = re.compile(r"(^|[\s_-])(menu|nav|navbar|navigation|footer|header|sidebar|breadcrumbs?|comments?|share|social|banner|cookie|modal|popup|widget|related|adv|ads)([\s_-]|$)", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote", "dd", "dt"}
MAX_TEXT = 400_000


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.h1: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False
        self._len = 0

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "h1":
            self._in_h1 = True
        if tag in _SKIP_TAGS:
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in _VOID:
            if tag == "br":
                self.parts.append("\n")
            return
        a = dict(attrs)
        marker = f"{a.get('class') or ''} {a.get('id') or ''} {a.get('role') or ''}"
        if tag in ("nav", "header", "footer", "aside") or (marker.strip() and _BOILER_RX.search(marker)):
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if self._skip_tag or self._len > MAX_TEXT:
            return
        if self._in_h1:
            self.h1.append(data)
        self.parts.append(data)
        self._len += len(data)


def html_text(raw: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(raw)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    title = re.sub(r"\s+", " ", "".join(p.title)).strip()
    h1 = re.sub(r"\s+", " ", "".join(p.h1)).strip()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return (h1 or title), text


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    url UNINDEXED, title, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS indexed (url TEXT PRIMARY KEY, sha1 TEXT, rowid_ INTEGER);
"""


def build_index(data_dir: str, rebuild: bool = False) -> None:
    state = sqlite3.connect(os.path.join(data_dir, "state.sqlite"), timeout=60)
    state.row_factory = sqlite3.Row
    path = os.path.join(data_dir, "search.sqlite")
    if rebuild and os.path.exists(path):
        os.remove(path)
    idx = sqlite3.connect(path)
    idx.executescript(SCHEMA)
    known = {r[0]: (r[1], r[2]) for r in idx.execute("SELECT url, sha1, rowid_ FROM indexed")}
    mirror = os.path.join(data_dir, "mirror")
    n = added = 0
    t0 = time.time()
    cur = state.execute(
        "SELECT url, path, sha1, content_type FROM urls WHERE status='done' AND path IS NOT NULL"
        " AND (content_type LIKE 'text/html%' OR content_type LIKE 'application/xhtml%')"
    )
    idx.execute("BEGIN")
    for row in cur:
        n += 1
        prev = known.get(row["url"])
        if prev and prev[0] == row["sha1"]:
            continue
        try:
            with open(mirror_file(mirror, row["path"]), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        title, text = html_text(decode_body(raw, row["content_type"]))
        if not text and not title:
            continue
        if prev:
            idx.execute("DELETE FROM docs WHERE rowid=?", (prev[1],))
        c = idx.execute("INSERT INTO docs(url, title, body) VALUES(?,?,?)", (row["url"], title, text))
        idx.execute("INSERT OR REPLACE INTO indexed(url, sha1, rowid_) VALUES(?,?,?)", (row["url"], row["sha1"], c.lastrowid))
        added += 1
        if added % 2000 == 0:
            idx.execute("COMMIT")
            log.info("индекс: обработано %d, добавлено %d (%.0f стр/с)", n, added, n / max(time.time() - t0, 1))
            idx.execute("BEGIN")
    idx.execute("COMMIT")
    log.info("индекс готов: страниц %d, обновлено %d. Оптимизация…", n, added)
    idx.execute("INSERT INTO docs(docs) VALUES('optimize')")
    idx.commit()
    idx.close()
    log.info("поиск: %s", path)


_WORD = re.compile(r"\w+", re.U)


def make_query(q: str) -> str:
    words = _WORD.findall(q)
    if not words:
        return '""'
    phrase = q.strip()
    if len(phrase) > 2 and phrase.startswith('"') and phrase.endswith('"'):
        return '"' + " ".join(words) + '"'
    return " AND ".join(f'"{w}"*' for w in words)


def search(path: str, q: str, limit: int = 30, offset: int = 0) -> tuple[int, list[dict]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    mq = make_query(q)
    total = conn.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH ?", (mq,)).fetchone()[0]
    rows = conn.execute(
        "SELECT url, title, snippet(docs, 2, '<mark>', '</mark>', ' … ', 24) AS snippet"
        " FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, 0.0, 8.0, 1.0) LIMIT ? OFFSET ?",
        (mq, limit, offset),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        # сниппет — текст страницы: экранируем всё, кроме наших <mark>
        s = html.escape(d["snippet"] or "").replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")
        d["snippet"] = s
        out.append(d)
    return total, out
