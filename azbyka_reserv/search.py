"""Полнотекстовый поиск по архиву (SQLite FTS5, без внешних зависимостей)."""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
from html.parser import HTMLParser

from .extract import decode_body
from .fsutil import fs_path, mirror_file

log = logging.getLogger("azbyka_reserv")

# Содержимое этих тегов в индекс не попадает никогда.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "object", "button", "select", "form",
              "nav", "footer", "aside"}
# Эвристика «служебного блока» по class/id/role применяется только к этим
# контейнерам — но не к html/body/main/article/section: у azbyka.ru на
# <html class="sidebar-toggle"> и <body class="… sidebar-hide …"> висят
# классы состояния, и раньше из-за них пропускалась вся страница.
_BOILER_TAGS = {"div", "span", "p", "ul", "ol", "li", "dl", "menu", "aside", "table", "tbody", "tr", "td"}
_BOILER_WORDS = {"menu", "nav", "navbar", "navigation", "footer", "header", "sidebar", "breadcrumbs", "breadcrumb",
                 "comments", "share", "social", "banner", "cookie", "modal", "popup", "widget", "related", "adv", "ads"}
_BOILER_EXACT = _BOILER_WORDS | {"comment-list"}
# «menu-toggle», «sidebar-hide», «nav-open» — классы состояния, а не сам блок
_STATE_SUFFIX = {"toggle", "toggler", "toggled", "hide", "hidden", "show", "shown", "open", "opened", "closed",
                 "collapse", "collapsed", "expanded", "active", "visible", "fixed", "mode", "on", "off",
                 "enabled", "disabled"}
# «has-sidebar», «no-sidebar», «with-sidebar» — тоже состояние обёртки
_STATE_PREFIX = {"is", "has", "no", "not", "with", "without", "show", "hide", "open", "toggle"}
# «entry-header», «post-header» — шапка статьи с заголовком, это содержимое
_CONTENT_WORDS = {"entry", "post", "page", "article", "content", "single", "book", "chapter", "text", "story"}
_BOILER_ROLES = {"navigation", "banner", "contentinfo", "complementary", "menu", "menubar", "dialog", "alertdialog"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
         "keygen", "frame", "basefont", "isindex"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote", "dd",
          "dt", "main", "header", "table", "ul", "ol", "pre", "figure", "figcaption"}
_CELL = {"td", "th"}
# открытый <p> неявно закрывается началом блочного элемента
_P_CLOSERS = {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figcaption", "figure",
              "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "menu", "nav", "ol", "p",
              "pre", "section", "table", "ul"}
_SPLIT_RX = re.compile(r"[-_]+")
MAX_TEXT = 400_000
_MAX_STACK = 1000


def _boiler_token(tok: str) -> bool:
    if tok in _BOILER_EXACT:
        return True
    parts = [p for p in _SPLIT_RX.split(tok) if p]
    if len(parts) < 2:
        return False
    first, last = parts[0], parts[-1]
    if first in _BOILER_WORDS and last not in _STATE_SUFFIX:
        return True
    if last in _BOILER_WORDS and first not in _STATE_PREFIX:
        if last == "header" and first in _CONTENT_WORDS:
            return False
        return True
    return False


def is_boilerplate(tag: str, attrs: dict) -> bool:
    """Похож ли контейнер на меню/шапку/подвал/боковую колонку по class/id/role."""
    if tag not in _BOILER_TAGS:
        return False
    role = (attrs.get("role") or "").strip().lower()
    if role == "main":
        return False
    if role in _BOILER_ROLES:
        return True
    for name in ("class", "id"):
        val = attrs.get(name)
        if val:
            for tok in val.lower().split():
                if _boiler_token(tok):
                    return True
    return False


class _Text(HTMLParser):
    """Текст страницы без служебных блоков.

    Ведётся упрощённый стек открытых тегов: пропускаемый блок заканчивается,
    когда снят со стека его элемент — своим закрывающим тегом или неявно
    (закрылся предок, </body>, </html>). Поэтому один незакрытый служебный
    тег не «съедает» остаток страницы.
    """

    def __init__(self, heuristics: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.heuristics = heuristics
        self.parts: list[str] = []
        self.title: list[str] = []
        self.h1: list[str] = []
        self.skipped_by_class = 0  # сколько текста отброшено эвристикой class/id
        self._stack: list[str] = []
        self._skip_at: int | None = None  # глубина элемента, с которого начат пропуск
        self._skip_by_class = False
        self._in_title = False
        self._title_done = False
        self._in_h1 = False
        self._h1_done = False
        self._noindex = 0  # внутри <!--noindex-->…<!--/noindex--> (соглашение Яндекса)
        self._len = 0

    # -- стек -----------------------------------------------------------------
    def _pop(self) -> None:
        tag = self._stack.pop()
        if tag == "title":
            self._in_title = False
            self._title_done = self._title_done or bool("".join(self.title).strip())
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = bool("".join(self.h1).strip())
        if self._skip_at is not None and len(self._stack) <= self._skip_at:
            self._skip_at = None

    def _pop_to(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag:
                while len(self._stack) > i:
                    self._pop()
                return
        # закрывающий тег без открывающего — игнорируем

    def _implicit_close(self, tag: str) -> None:
        st = self._stack
        if not st:
            return
        top = st[-1]
        if top == "p" and tag in _P_CLOSERS:
            self._pop()
        elif tag == "li" and top == "li":
            self._pop()
        elif tag in ("dt", "dd") and top in ("dt", "dd"):
            self._pop()
        elif tag in _CELL and top in _CELL:
            self._pop()
        elif tag == "tr":
            while st and st[-1] in _CELL:
                self._pop()
            if st and st[-1] == "tr":
                self._pop()
        elif tag == "option" and top == "option":
            self._pop()

    def _start_skip(self, by_class: bool) -> None:
        self._skip_at = len(self._stack) - 1
        self._skip_by_class = by_class

    # -- события парсера ------------------------------------------------------
    def _start(self, tag: str, attrs) -> bool:
        """Обрабатывает открывающий тег; True — элемент положен на стек."""
        if tag in _VOID:
            if tag == "br" and self._skip_at is None:
                self.parts.append("\n")
            return False
        if tag == "body":
            # служебный блок, открытый ещё в <head>, на тело страницы не распространяется
            self._skip_at = None
            self._noindex = 0
        self._implicit_close(tag)
        a: dict[str, str] = {}
        for k, v in attrs:
            if v is not None and k not in a:
                a[k] = v
        if self._skip_at is not None:
            # <main> внутри «служебного» блока — значит, эвристика ошиблась
            if self._skip_by_class and (tag == "main" or (a.get("role") or "").lower() == "main"):
                self._skip_at = None
            else:
                if len(self._stack) >= _MAX_STACK:
                    return False
                self._stack.append(tag)
                return True
        if len(self._stack) >= _MAX_STACK:
            return False  # патологическая вложенность: дальше без учёта стека
        self._stack.append(tag)
        if tag == "title":
            if not self._title_done:
                self._in_title = True
            return True
        if tag in _SKIP_TAGS:
            self._start_skip(False)
            return True
        if tag == "header" and not any(t in ("article", "main") for t in self._stack[:-1]):
            self._start_skip(False)  # шапка сайта; <header> внутри статьи — с заголовком, оставляем
            return True
        if self.heuristics and is_boilerplate(tag, a):
            self._start_skip(True)
            return True
        if tag == "h1" and not self._h1_done:
            self._in_h1 = True
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")
        return True

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        if self._start(tag, attrs):  # <div/> — сразу и закрыт
            self._pop()

    def handle_endtag(self, tag):
        if tag in ("body", "html"):
            # конец документа: никакой пропуск не продолжается дальше
            self._skip_at = None
            self._noindex = 0
            if tag in self._stack:
                self._pop_to(tag)
            return
        if tag in _VOID:
            return
        self._pop_to(tag)

    def handle_comment(self, data):
        if not self.heuristics:
            return
        mark = data.strip().lower()
        if mark == "noindex":
            self._noindex += 1
        elif mark == "/noindex" and self._noindex:
            self._noindex -= 1

    def handle_data(self, data):
        if self._in_title and self._skip_at is None:
            self.title.append(data)
            return
        if self._skip_at is not None or self._noindex:
            if self._skip_by_class or self._noindex:
                self.skipped_by_class += len(data)
            return
        if self._len > MAX_TEXT:
            return
        if self._in_h1:
            self.h1.append(data)
        self.parts.append(data)
        self._len += len(data)


def _parse(raw: str, heuristics: bool) -> _Text:
    p = _Text(heuristics)
    try:
        p.feed(raw)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    return p


def _clean(parts: list[str]) -> str:
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", "".join(parts))
    return re.sub(r"\n\s*", "\n", text).strip()


def html_text(raw: str) -> tuple[str, str]:
    """(заголовок, текст) страницы без меню, шапки, подвала и боковых колонок."""
    p = _parse(raw, True)
    text = _clean(p.parts)
    if len(text) < 500 and p.skipped_by_class > max(2000, 4 * len(text)):
        # эвристика выкинула почти всё — вероятно, ошиблась; берём без неё
        p2 = _parse(raw, False)
        text2 = _clean(p2.parts)
        if len(text2) > len(text):
            p, text = p2, text2
    title = re.sub(r"\s+", " ", "".join(p.title)).strip()
    h1 = re.sub(r"\s+", " ", "".join(p.h1)).strip()
    return (h1 or title), text


# -- нормализация текста ---------------------------------------------------------
# Одна и та же для индекса и для запроса: ударения (U+0301), титла и прочие
# надстрочные знаки церковнославянского (категория Mn) снимаются, ё -> е.
# Исключение — кратка (U+0306) над и/у: «й» и «ў» — отдельные буквы
# (FTS5 их тоже не сводит к «и»), иначе «Сергий» в выдаче стал бы «Сергии».
_BREVE = "\u0306"
_STRAY_BREVE_RX = re.compile(r"(?<![иИуУ])\u0306")
_MN_TABLE: dict[int, str | None] | None = None


def _mn_table() -> dict[int, str | None]:
    global _MN_TABLE
    if _MN_TABLE is None:
        t: dict[int, str | None] = {
            cp: None for cp in range(sys.maxunicode + 1)
            if cp != 0x306 and unicodedata.category(chr(cp)) == "Mn"
        }
        t[ord("ё")] = "е"
        t[ord("Ё")] = "Е"
        _MN_TABLE = t
    return _MN_TABLE


def norm_text(s: str) -> str:
    if not s or s.isascii():
        return s
    s = unicodedata.normalize("NFD", s).translate(_mn_table())
    if _BREVE in s:
        s = _STRAY_BREVE_RX.sub("", s)
    return unicodedata.normalize("NFC", s).replace("ё", "е").replace("Ё", "Е")


# -- служебное: SQLite URI и пути ----------------------------------------------
def sqlite_uri(path: str, mode: str = "ro") -> str:
    """URI для sqlite3.connect(..., uri=True), устойчивый к '#', '%', '?', пробелам, кириллице.

    file:///home/x/state.sqlite, file:///C:/x/state.sqlite, file:////server/share/state.sqlite
    """
    p = os.path.abspath(path)
    if os.name == "nt":
        if p.startswith("\\\\?\\UNC\\"):
            p = "\\\\" + p[8:]
        elif p.startswith("\\\\?\\"):
            p = p[4:]
        p = p.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p  # C:/x -> /C:/x
    return "file://" + urllib.parse.quote(p, safe="/:") + "?mode=" + mode


_CI_CACHE: dict[tuple[str, str], str] = {}


def find_ci(root: str, relpath: str) -> str | None:
    """Путь к файлу ``root/relpath`` без учёта регистра имён.

    На NTFS папки, различающиеся только регистром, сливаются в одну, и после
    переноса архива на Linux путь из базы может не совпасть буквально.
    """
    cur = root
    for seg in relpath.split("/"):
        if not seg or seg in (".", ".."):
            return None
        key = (cur, seg)
        hit = _CI_CACHE.get(key)
        if hit is None:
            if os.path.lexists(fs_path(os.path.join(cur, seg))):
                hit = seg
            else:
                want = unicodedata.normalize("NFC", seg).casefold()
                try:
                    with os.scandir(fs_path(cur)) as it:
                        for e in it:
                            if unicodedata.normalize("NFC", e.name).casefold() == want:
                                hit = e.name
                                break
                except OSError:
                    return None
                if hit is None:
                    return None
            if len(_CI_CACHE) > 20_000:
                _CI_CACHE.clear()
            _CI_CACHE[key] = hit
        cur = os.path.join(cur, hit)
    return fs_path(cur)


def read_mirror(mirror: str, relpath: str) -> bytes:
    try:
        with open(mirror_file(mirror, relpath), "rb") as f:
            return f.read()
    except OSError:
        alt = find_ci(mirror, relpath)
        if alt is None:
            raise
        with open(alt, "rb") as f:
            return f.read()


# -- индекс ----------------------------------------------------------------------
SCHEMA_VERSION = "2"
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    url UNINDEXED, title, body, otitle UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3'
);
CREATE TABLE IF NOT EXISTS indexed (url TEXT PRIMARY KEY, sha1 TEXT, rowid_ INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
CHUNK = 2000
OPTIMIZE_AFTER = 10_000
# страница пропала из архива (а не просто стоит в очереди на обновление)
_GONE = ("redirect", "notfound", "skipped", "auth", "gone")
_HTML_WHERE = "(content_type LIKE 'text/html%' OR content_type LIKE 'application/xhtml%')"


def _schema_version(path: str) -> str | None:
    """Версия схемы существующего индекса; None — файла нет или он пустой."""
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(sqlite_uri(path), uri=True)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "meta" in names:
                row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if row:
                    return row[0]
            return "1" if "docs" in names else None
        finally:
            conn.close()
    except sqlite3.Error:
        return "?"


def _drop_index(path: str) -> None:
    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    except OSError:
        # файл занят (Windows: открыт просмотрщиком) — очищаем изнутри
        conn = sqlite3.connect(path, timeout=60)
        conn.executescript("DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS indexed; DROP TABLE IF EXISTS meta;")
        conn.close()


def build_index(data_dir: str, rebuild: bool = False) -> None:
    path = os.path.join(data_dir, "search.sqlite")
    ver = _schema_version(path)
    if not rebuild and ver is not None and ver != SCHEMA_VERSION:
        log.info("индекс поиска старого формата (версия %s) — перестраиваю целиком", ver)
        rebuild = True
    if rebuild:
        _drop_index(path)
    idx = sqlite3.connect(path, isolation_level=None)
    idx.executescript(SCHEMA)
    idx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (SCHEMA_VERSION,))
    # Чтение state.sqlite — короткими порциями по id: одна долгая выборка
    # держала бы снимок базы часами и не давала обходчику сбрасывать WAL.
    state = sqlite3.connect(os.path.join(data_dir, "state.sqlite"), timeout=60)
    state.row_factory = sqlite3.Row
    mirror = os.path.join(data_dir, "mirror")
    n = changed = 0
    t0 = last_log = time.time()
    last_id = 0
    try:
        while True:
            rows = state.execute(
                "SELECT id, url, path, sha1, content_type FROM urls WHERE id > ? AND status='done'"
                f" AND path IS NOT NULL AND {_HTML_WHERE} ORDER BY id LIMIT ?",
                (last_id, CHUNK),
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1]["id"]
            idx.execute("BEGIN")
            for row in rows:
                n += 1
                changed += _index_one(idx, mirror, row)
            idx.execute("COMMIT")
            if time.time() - last_log > 30:
                last_log = time.time()
                log.info("индекс: просмотрено %d, обновлено %d (%.0f стр/с)", n, changed, n / max(time.time() - t0, 1))
        removed = 0 if rebuild else _drop_stale(idx, state)
        changed += removed
        log.info("индекс готов: страниц %d, обновлено %d, удалено устаревших %d", n, changed - removed, removed)
        if rebuild or changed > OPTIMIZE_AFTER:
            log.info("оптимизация индекса…")
            idx.execute("INSERT INTO docs(docs) VALUES('optimize')")
    finally:
        state.close()
        idx.close()
    log.info("поиск: %s", path)


def _index_one(idx: sqlite3.Connection, mirror: str, row: sqlite3.Row) -> int:
    url, sha1 = row["url"], row["sha1"]
    prev = idx.execute("SELECT sha1, rowid_ FROM indexed WHERE url=?", (url,)).fetchone()
    if prev is not None and prev[0] == sha1:
        return 0
    try:
        raw = read_mirror(mirror, row["path"])
    except OSError:
        return 0
    title, text = html_text(decode_body(raw, row["content_type"]))
    if prev is not None and prev[1] is not None:
        idx.execute("DELETE FROM docs WHERE rowid=?", (prev[1],))
    rowid = None
    if text or title:
        c = idx.execute("INSERT INTO docs(url, title, body, otitle) VALUES(?,?,?,?)",
                        (url, norm_text(title), norm_text(text), title))
        rowid = c.lastrowid
    # пустые страницы тоже запоминаем, чтобы не перечитывать их каждый раз
    idx.execute("INSERT OR REPLACE INTO indexed(url, sha1, rowid_) VALUES(?,?,?)", (url, sha1, rowid))
    return 1


def _drop_stale(idx: sqlite3.Connection, state: sqlite3.Connection) -> int:
    """Убирает из индекса страницы, которых в архиве больше нет."""
    removed = 0
    last = ""
    while True:
        rows = idx.execute("SELECT url, rowid_ FROM indexed WHERE url > ? ORDER BY url LIMIT ?", (last, CHUNK)).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        stale = []
        for url, rowid in rows:
            st = state.execute(f"SELECT status, path, {_HTML_WHERE} AS is_html FROM urls WHERE url=?", (url,)).fetchone()
            if st is None or st["status"] in _GONE or (st["status"] == "done" and (st["path"] is None or not st["is_html"])):
                stale.append((url, rowid))
        if stale:
            idx.execute("BEGIN")
            for url, rowid in stale:
                if rowid is not None:
                    idx.execute("DELETE FROM docs WHERE rowid=?", (rowid,))
                idx.execute("DELETE FROM indexed WHERE url=?", (url,))
            idx.execute("COMMIT")
            removed += len(stale)
    return removed


# -- запросы ---------------------------------------------------------------------
_WORD = re.compile(r"\w+", re.U)
_OPEN_QUOTES = '"«“„'
_CLOSE_QUOTES = '"»”“'
COUNT_CAP = 1000  # больше не считаем: в просмотрщике показывается «1000+»


def make_query(q: str) -> str | None:
    """Строка запроса FTS5 или None, если искать нечего.

    Запрос нормализуется так же, как текст в индексе. Слова от 3 букв ищутся
    по началу (``"слово"*``), однобуквенные отбрасываются (кроме фразы в кавычках).
    """
    nq = norm_text(q).strip()
    words = _WORD.findall(nq)
    if len(nq) > 2 and nq[0] in _OPEN_QUOTES and nq[-1] in _CLOSE_QUOTES:
        return ('"' + " ".join(words) + '"') if words else None
    words = [w for w in words if len(w) > 1]
    if not words:
        return None
    return " AND ".join(f'"{w}"*' if len(w) >= 3 else f'"{w}"' for w in words)


def search(path: str, q: str, limit: int = 30, offset: int = 0) -> tuple[int, list[dict]]:
    """(число найденных, но не больше COUNT_CAP + 1; результаты страницы)."""
    mq = make_query(q)
    if mq is None:
        return 0, []
    conn = sqlite3.connect(sqlite_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM docs WHERE docs MATCH ? LIMIT ?)", (mq, COUNT_CAP + 1)
        ).fetchone()[0]
        sql = ("SELECT url, {title} AS title, snippet(docs, 2, '<mark>', '</mark>', ' … ', 24) AS snippet"
               " FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, {w}) LIMIT ? OFFSET ?")
        try:
            rows = conn.execute(sql.format(title="COALESCE(NULLIF(otitle, ''), title)", w="0.0, 8.0, 1.0, 0.0"),
                                (mq, limit, offset)).fetchall()
        except sqlite3.OperationalError:  # индекс старого формата (без otitle)
            rows = conn.execute(sql.format(title="title", w="0.0, 8.0, 1.0"), (mq, limit, offset)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        # сниппет — текст страницы: экранируем всё, кроме наших <mark>
        s = html.escape(d["snippet"] or "").replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")
        d["snippet"] = s
        out.append(d)
    return total, out
