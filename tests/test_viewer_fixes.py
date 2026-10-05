"""Просмотрщик, поиск и каталог: регрессии найденных дефектов."""

import glob
import hashlib
import http.client
import os
import re
import sqlite3
import threading
import urllib.parse

import pytest

from azbyka_reserv import catalog as catalog_mod
from azbyka_reserv import search as search_mod
from azbyka_reserv.catalog import build_catalog
from azbyka_reserv.search import (COUNT_CAP, SCHEMA_VERSION, build_index, html_text, make_query, norm_text, search,
                                  sqlite_uri)
from azbyka_reserv.serve import Archive, make_server, safe_video_path

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "probe", "live-samples")

SCHEMA = """
CREATE TABLE urls (
    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, alt_url TEXT, kind TEXT NOT NULL DEFAULT 'page',
    priority INTEGER NOT NULL DEFAULT 50, depth INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
    http_status INTEGER, content_type TEXT, size INTEGER, path TEXT, path_key TEXT UNIQUE, sha1 TEXT, etag TEXT,
    last_modified TEXT, location TEXT, title TEXT, tries INTEGER NOT NULL DEFAULT 0, error TEXT, parent_id INTEGER,
    discovered_at REAL, fetched_at REAL
);
CREATE TABLE embeds (url TEXT PRIMARY KEY, page_url TEXT, status TEXT NOT NULL DEFAULT 'new', path TEXT, error TEXT);
"""


def weird_dir(tmp_path, name="Архив #1 100%"):
    if os.name != "nt":
        name += " ?x"
    return str(tmp_path / name / "data")


class Arch:
    """Маленький архив, собранный вручную (без обходчика)."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.mirror = os.path.join(data, "mirror")
        os.makedirs(self.mirror, exist_ok=True)
        self.conn = sqlite3.connect(os.path.join(data, "state.sqlite"))
        self.conn.executescript(SCHEMA)

    def write(self, relpath: str, body: bytes) -> None:
        full = os.path.join(self.mirror, *relpath.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(body)

    def add(self, url, body=None, ctype="text/html; charset=utf-8", status="done", path=None, location=None,
            kind="page", disk_path=None, parent_id=None, title=None) -> int:
        sha1 = None
        if body is not None:
            if isinstance(body, str):
                body = body.encode("utf-8")
            path = path or url.split("://", 1)[1].split("/", 1)[1].split("?")[0].rstrip("/") + ".html"
            self.write(disk_path or path, body)
            sha1 = hashlib.sha1(body).hexdigest()
        cur = self.conn.execute(
            "INSERT INTO urls(url, kind, status, content_type, size, path, path_key, sha1, location, parent_id, title)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (url, kind, status, ctype if status == "done" else None, len(body) if body is not None else None,
             path, path.lower() if path else None, sha1, location, parent_id, title),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_body(self, url: str, body: str) -> None:
        path = self.conn.execute("SELECT path FROM urls WHERE url=?", (url,)).fetchone()[0]
        raw = body.encode("utf-8")
        self.write(path, raw)
        self.conn.execute("UPDATE urls SET sha1=?, size=? WHERE url=?", (hashlib.sha1(raw).hexdigest(), len(raw), url))
        self.conn.commit()


# -- извлечение текста -----------------------------------------------------------

WP_PAGE = """<!doctype html><html lang="ru" class="sidebar-toggle"><head><title>Сайт — Статья</title></head>
<body class="post-template single sidebar-hide sidebar-toggle" id="main-body">
<header class="site-header"><nav class="main-menu"><ul><li>МЕНЮОДИН</li></ul></nav>ШАПКАТЕКСТ</header>
<div class="content-sidebar-wrap has-sidebar">
<main id="main"><article class="post">
<header class="entry-header"><h1 class="entry-title">Заголовок статьи</h1></header>
<div class="entry-content"><p>Основной ТЕКСТ статьи о молитве.</p><p>Второй абзац.</p></div>
<div class="share-buttons">ПОДЕЛИТЬСЯ</div>
</article></main>
<aside class="sidebar">БОКОВАЯТЕКСТ</aside>
</div>
<!--noindex--><div class="settings-wrap">Размер шрифта</div><!--/noindex-->
<footer class="site-footer">ПОДВАЛТЕКСТ</footer>
</body></html>"""


def test_html_text_wordpress_layout():
    title, text = html_text(WP_PAGE)
    assert title == "Заголовок статьи"
    assert "Основной ТЕКСТ статьи" in text and "Второй абзац" in text
    for junk in ("МЕНЮОДИН", "ШАПКАТЕКСТ", "БОКОВАЯТЕКСТ", "ПОДВАЛТЕКСТ", "ПОДЕЛИТЬСЯ", "Размер шрифта"):
        assert junk not in text, junk


def test_html_text_unclosed_boilerplate():
    # <ul class="menu"> не закрыт: пропуск кончается вместе с родительским <div>
    title, text = html_text('<html><body><div class="x"><ul class="menu"><li>ПУНКТ<li>ПУНКТ2</div>'
                            "<p>ВИДНО1</p><div class='sidebar'><div>вложенный незакрытый</div><p>ВИДНО2</p></body></html>")
    assert "ВИДНО1" in text and "ПУНКТ" not in text
    # обёртка с «боковым» классом, внутри которой <main>, — эвристика ошиблась
    _, text = html_text('<body><div class="layout-sidebar"><main><p>ГЛАВНОЕ</p></main></div></body>')
    assert "ГЛАВНОЕ" in text
    # служебный блок, открытый в <head>, не съедает тело страницы
    _, text = html_text('<html><head><div class="menu">M</head><body><p>ТЕЛО</p></body></html>')
    assert "ТЕЛО" in text
    # одна лишь эвристика class выкинула почти всё — берём текст без неё
    big = "слово " * 1000
    _, text = html_text(f'<body><div class="wrap sidebar-left"><p>{big}</p></div></body>')
    assert len(text) > 3000


@pytest.mark.skipif(not os.path.isdir(SAMPLES), reason="нет probe/live-samples")
def test_html_text_real_pages():
    expect = {
        "azbyka.ru_vopros_spat-valetom-ili-valtom_.html": ("Лесков", 300),
        "azbyka.ru_molitvoslov_akafist-svyashhennomucheniku-sergiyu-mechevu-presviteru-moskovskomu.html.html": ("Кондак", 5000),
        "azbyka.ru_fiction_alaya-chuma_.html": ("", 50_000),
        "azbyka.ru_otechnik_Antonij_Surozhskij_uchites-molitsja_.html": ("", 50_000),
        "azbyka.ru_worships_.html": ("", 10_000),
    }
    seen = 0
    for name, (needle, min_len) in expect.items():
        path = os.path.join(SAMPLES, name)
        if not os.path.exists(path):
            continue
        seen += 1
        with open(path, encoding="utf-8", errors="replace") as f:
            title, text = html_text(f.read())
        assert title, name
        assert len(text) >= min_len, (name, len(text))
        assert needle in text, name
        assert "Размер шрифта" not in text, name
    # ни одна страница из образцов не должна оказаться пустой
    for path in glob.glob(os.path.join(SAMPLES, "*.html")):
        with open(path, encoding="utf-8", errors="replace") as f:
            assert len(html_text(f.read())[1]) > 300, path
    assert seen


# -- нормализация и запросы -----------------------------------------------------

def test_norm_text_and_make_query():
    assert norm_text("Христо́с воскре́се") == "Христос воскресе"
    assert norm_text("Ёлка ещё") == "Елка еще"
    assert norm_text("Сергий Мечёв, бой") == "Сергий Мечев, бой"  # й остаётся буквой
    assert norm_text("Бг҃ъ ѿ") == "Бгъ ѿ"  # титло снято
    assert norm_text("café") == "cafe"
    assert make_query("в Москве") == '"Москве"*'
    assert make_query("Мф 1") == '"Мф"'
    assert make_query("Мечёву") == '"Мечеву"*'
    assert make_query('"в начале"') == '"в начале"'
    assert make_query("«в начале»") == '"в начале"'
    assert make_query("а") is None and make_query("  ") is None


def test_sqlite_uri():
    if os.name == "nt":
        assert sqlite_uri("C:\\a #b\\x.sqlite") == "file:///C:/a%20%23b/x.sqlite?mode=ro"
        assert sqlite_uri("\\\\srv\\share\\x.sqlite").startswith("file:////srv/share/x.sqlite")
    else:
        assert sqlite_uri("/tmp/a#b%c?d/Ж.sqlite") == "file:///tmp/a%23b%25c%3Fd/%D0%96.sqlite?mode=ro"


# -- индекс -----------------------------------------------------------------------

def _urls(hits):
    return [h["url"] for h in hits]


def test_index_incremental_and_weird_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "CHUNK", 2)  # несколько порций чтения state.sqlite
    data = weird_dir(tmp_path)
    ar = Arch(data)
    ar.add("https://azbyka.ru/a", '<html class="sidebar-toggle"><head><title>Акафист</title></head>'
           '<body class="sidebar-hide"><p>Акафист священномученику Сергию Мечёву</p></body></html>')
    ar.add("https://azbyka.ru/b", "<html><body><p>Христо́с воскре́се из ме́ртвых</p></body></html>")
    ar.add("https://azbyka.ru/c", "<html><body><p>Ещё одна страница про ёлку</p></body></html>")
    ar.add("https://azbyka.ru/d", "<html><body><p>Пятая страница</p></body></html>")
    ar.add("https://azbyka.ru/e", "<html><body></body></html>")
    build_index(data)
    idx = os.path.join(data, "search.sqlite")

    total, hits = search(idx, "Мечёву")
    assert total == 1 and _urls(hits) == ["https://azbyka.ru/a"]
    assert hits[0]["title"] == "Акафист"
    assert search(idx, "мечев")[0] == 1  # по началу слова, ё == е
    assert _urls(search(idx, "воскресе")[1]) == ["https://azbyka.ru/b"]
    assert _urls(search(idx, "Христос")[1]) == ["https://azbyka.ru/b"]
    assert _urls(search(idx, "елку")[1]) == ["https://azbyka.ru/c"]
    assert search(idx, "а") == (0, [])

    # изменённая страница переиндексируется, пропавшая — удаляется
    ar.update_body("https://azbyka.ru/a", "<html><body><p>Теперь здесь про Кронштадт</p></body></html>")
    ar.conn.execute("UPDATE urls SET status='notfound' WHERE url='https://azbyka.ru/b'")
    ar.conn.commit()
    build_index(data)
    assert search(idx, "Мечёву")[0] == 0
    assert _urls(search(idx, "Кронштадт")[1]) == ["https://azbyka.ru/a"]
    assert search(idx, "воскресе")[0] == 0
    assert search(idx, "елку")[0] == 1

    # индекс старой версии перестраивается целиком сам
    c = sqlite3.connect(idx)
    c.execute("UPDATE meta SET value='1' WHERE key='schema'")
    c.execute("INSERT INTO docs(url, title, body) VALUES('https://azbyka.ru/zz', 'x', 'мусорноеслово')")
    c.commit()
    c.close()
    assert search(idx, "мусорноеслово")[0] == 1
    build_index(data)
    assert search(idx, "мусорноеслово")[0] == 0
    assert search(idx, "Кронштадт")[0] == 1
    c = sqlite3.connect(idx)
    assert c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == SCHEMA_VERSION
    c.close()


# -- просмотрщик ------------------------------------------------------------------

MP3 = bytes(range(256)) * 4  # 1024 байта


@pytest.fixture()
def viewer(tmp_path):
    data = weird_dir(tmp_path, "Просмотр #2 5%")
    ar = Arch(data)
    A = "https://azbyka.ru"
    # редиректы: /x <-> /x/ — цикл; /y -> /y/ (скачано); /z -> /z/ (нет в архиве)
    ar.add(A + "/x", status="redirect", location=A + "/x/")
    ar.add(A + "/x/", status="redirect", location=A + "/x")
    ar.add(A + "/y", status="redirect", location=A + "/y/")
    ar.add(A + "/y/", "<html><body><p>Страница Y</p></body></html>", path="y/index.html")
    ar.add(A + "/z", status="redirect", location=A + "/z/")
    ar.add(A + "/redir2", status="redirect", location=A + "/y/")
    # страница в windows-1251 с байтом, которого нет в этой кодировке
    cp = ('<html><head><meta charset="windows-1251"></head><body><p>Привет</p>'
          '<a href="https://azbyka.ru/q">q</a>').encode("cp1251") + b"\x98" + b"</body></html>"
    ar.add(A + "/cp", cp, ctype="text/html; charset=windows-1251")
    # плейлист, сохранённый как octet-stream
    ar.add(A + "/audio/list.m3u", "#EXTM3U\n#EXTINF:1,Трек\nt1.mp3\r\nhttps://azbyka.ru/audio/t2.mp3\n"
           "http://cdn.example.com/x.mp3\n", ctype="application/octet-stream", path="audio/list.m3u", kind="media")
    ar.add(A + "/files/t.mp3", MP3, ctype="audio/mpeg", path="files/t.mp3", kind="media")
    # встроенное видео YouTube, скачанное командой video
    ar.add(A + "/v", '<html><body><iframe width=560 src="https://www.youtube.com/embed/abc123?rel=0"></iframe>'
           '<iframe src="https://maps.example.com/embed?x"></iframe></body></html>')
    vrel = "video/www.youtube.com_watch_v_abc123/Фильм #1 [abc123].mp4"
    os.makedirs(os.path.join(data, *vrel.split("/")[:-1]))
    with open(os.path.join(data, *vrel.split("/")), "wb") as f:
        f.write(MP3)
    ar.conn.execute("INSERT INTO embeds(url, page_url, status, path) VALUES(?,?,?,?)",
                    ("https://www.youtube.com/embed/abc123", A + "/v", "done", vrel))
    ar.conn.execute("INSERT INTO embeds(url, page_url, status) VALUES(?,?,?)",
                    ("https://rutube.ru/play/embed/zzz", A + "/v", "new"))
    ar.conn.commit()
    # кавычка в запросе: в базе «сырая», браузер шлёт %27 (и наоборот)
    ar.add(A + "/s?q=it's", "<html><body>S1</body></html>", path="s1.html")
    ar.add(A + "/t?q=it%27s", "<html><body>T1</body></html>", path="t1.html")
    # NTFS слил папки разного регистра: в базе Otechnik/Kniga, на диске otechnik/kniga
    ar.add(A + "/Otechnik/Kniga/1", "<html><body>Глава</body></html>", path="Otechnik/Kniga/Glava.html",
           disk_path="otechnik/kniga/glava.html")
    ar.conn.commit()
    # индекс с более чем COUNT_CAP совпадений — строим напрямую
    idx = sqlite3.connect(os.path.join(data, "search.sqlite"))
    idx.executescript(search_mod.SCHEMA)
    idx.executemany("INSERT INTO docs(url, title, body, otitle) VALUES(?,?,?,?)",
                    [(f"{A}/n{i}", f"Стр {i}", "общее слово", f"Стр {i}") for i in range(COUNT_CAP + 5)])
    idx.commit()
    idx.close()

    srv = make_server(data, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def get(path, headers=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    try:
        yield get, port, data
    finally:
        srv.shutdown()
        srv.server_close()


def test_redirects(viewer):
    get, _, _ = viewer
    r, _ = get("/x")
    assert r.status == 404  # цикл /x <-> /x/ — не 302 на самого себя
    r, _ = get("/x/")
    assert r.status == 404
    r, body = get("/y")
    assert r.status == 200 and "Страница Y".encode() in body  # скачанный вариант важнее редиректа
    r, _ = get("/z")
    assert r.status == 404  # /z -> /z/ (нет) -> снова /z: не зацикливаемся
    r, _ = get("/redir2")
    assert r.status == 302 and r.getheader("Location") == "/y/"


def test_charset_lossless(viewer):
    get, _, _ = viewer
    r, body = get("/cp")
    assert r.status == 200
    assert "Привет".encode("cp1251") in body and b"\x98" in body
    assert b'href="/q"' in body
    assert b"&#1054;" in body and body.isascii() is False
    assert "Офлайн".encode("cp1251") not in body  # значок — только ASCII


def test_playlist_absolute_urls(viewer):
    get, port, _ = viewer
    r, body = get("/audio/list.m3u")
    assert r.status == 200
    text = body.decode("utf-8")
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    origin = f"http://127.0.0.1:{port}"
    assert lines == [origin + "/audio/t1.mp3", origin + "/audio/t2.mp3", origin + "/__ext__/cdn.example.com/x.mp3"]
    assert "#EXTINF:1,Трек" in text
    assert "mpegurl" in r.getheader("Content-Type")


def test_embedded_video(viewer):
    get, _, data = viewer
    r, body = get("/v")
    assert b'src="/__ext__/www.youtube.com/embed/abc123?rel=0"' in body
    assert b"https://maps.example.com/embed" in body  # прочие iframe не трогаем
    r, body = get("/__ext__/www.youtube.com/embed/abc123?rel=0")
    assert r.status == 200
    m = re.search(rb'<video controls[^>]*src="([^"]+)"', body)
    assert m, body
    src = m.group(1).decode().replace("&amp;", "&")
    assert src.startswith("/__azr__/video/")
    r, part = get(src, {"Range": "bytes=10-19"})
    assert r.status == 206 and part == MP3[10:20]
    r, body = get("/__ext__/rutube.ru/play/embed/zzz")
    assert r.status == 200 and "Видео не скачано (команда video)".encode() in body
    # выход за пределы data/video
    for bad in ("/__azr__/video/../state.sqlite", "/__azr__/video/%2e%2e/state.sqlite",
                "/__azr__/video/x/..%2f..%2fstate.sqlite", "/__azr__/video/%2fetc%2fpasswd",
                "/__azr__/video/..%5c..%5cstate.sqlite", "/__azr__/video/"):
        r, body = get(bad)
        assert r.status == 404, bad
        assert b"SQLite format" not in body
    assert safe_video_path(data, "video/../state.sqlite") is None
    assert safe_video_path(data, "mirror/x") is None
    assert safe_video_path(data, "video/a/b.mp4") is not None


def test_range_and_params(viewer):
    get, _, _ = viewer
    r, _ = get("/files/t.mp3", {"Range": "bytes=5-2"})
    assert r.status == 416
    r, body = get("/files/t.mp3", {"Range": "bytes=0-1,5-6"})
    assert r.status == 200 and body == MP3
    r, body = get("/files/t.mp3", {"Range": "bytes=abc"})
    assert r.status == 200 and body == MP3
    r, body = get("/files/t.mp3", {"Range": "bytes=-10"})
    assert r.status == 206 and body == MP3[-10:]
    r, body = get("/files/t.mp3", {"Range": "bytes=1000-5000"})
    assert r.status == 206 and body == MP3[1000:]
    r, _ = get("/files/t.mp3", {"Range": "bytes=5000-"})
    assert r.status == 416
    r, body = get("/files/t.mp3", method="HEAD")
    assert r.status == 200 and body == b""
    for p in ("abc", "-5", "99999999999", ""):
        r, _ = get(f"/__azr__/search?q=%D1%81%D0%BB%D0%BE%D0%B2%D0%BE&p={p}")
        assert r.status == 200, p


def test_search_cap_quote_and_case(viewer):
    get, _, _ = viewer
    r, body = get("/__azr__/search?q=%D1%81%D0%BB%D0%BE%D0%B2%D0%BE")
    assert r.status == 200 and f"Найдено: {COUNT_CAP}+".encode() in body
    r, body = get("/s?q=it%27s")
    assert r.status == 200 and b"S1" in body
    r, body = get("/t?q=it's")
    assert r.status == 200 and b"T1" in body
    r, body = get("/Otechnik/Kniga/1")
    assert r.status == 200 and "Глава".encode() in body


def test_archive_weird_path_readonly(tmp_path):
    data = weird_dir(tmp_path, "Каталог #3 7%")
    ar = Arch(data)
    ar.add("https://azbyka.ru/p", "<html><body>x</body></html>")
    a = Archive(data)
    assert a.lookup("https://azbyka.ru/p")["status"] == "done"
    with pytest.raises(sqlite3.OperationalError):
        a.db().execute("UPDATE urls SET status='x'")  # только чтение


# -- каталог ------------------------------------------------------------------------

def test_catalog_cyrillic_and_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "CHUNK", 3)
    data = weird_dir(tmp_path, "Каталог #4 8%")
    ar = Arch(data)
    page = ar.add("https://azbyka.ru/otechnik/kniga/", "<html><title>Книга</title></html>", title="Книга о молитве")
    names = [f"Том {i} #{i} 100%.epub" for i in range(7)]
    for n in names:
        ar.add("https://azbyka.ru/otechnik/books/download/" + urllib.parse.quote(n), b"PK" + n.encode(),
               ctype="application/epub+zip", path="otechnik/books/" + n, kind="media", parent_id=page)
    ar.add("https://azbyka.ru/audio/Песнь.mp3", MP3, ctype="audio/mpeg", path="audio/Песнь.mp3", kind="media")
    os.makedirs(os.path.join(data, "video", "yt"))
    with open(os.path.join(data, "video", "yt", "Фильм [x].mp4"), "wb") as f:
        f.write(b"v")
    ar.conn.execute("INSERT INTO embeds(url, page_url, status, path) VALUES('https://youtu.be/x', NULL, 'done', ?)",
                    ("video/yt/Фильм [x].mp4",))
    ar.conn.commit()

    cat = build_catalog(data)
    cat_dir = os.path.dirname(cat)
    with open(os.path.join(cat_dir, "books-1.html"), encoding="utf-8") as f:
        books = f.read()
    hrefs = re.findall(r"href='([^']+)'", books)
    files = [h for h in hrefs if h.startswith("../mirror/")]
    assert len(files) == 7 and "Книга о молитве" in books
    for h in files:
        assert "#" not in h and " " not in h and "%D0" in h
        assert os.path.exists(os.path.join(cat_dir, *urllib.parse.unquote(h).split("/"))), h
    with open(os.path.join(cat_dir, "video-1.html"), encoding="utf-8") as f:
        vid = f.read()
    href = re.search(r"href='(\.\./video/[^']+)'", vid).group(1)
    assert os.path.exists(os.path.join(cat_dir, *urllib.parse.unquote(href).split("/")))
    with open(os.path.join(cat_dir, "files.csv"), encoding="utf-8-sig") as f:
        rows = f.read().splitlines()
    assert rows[0].startswith("категория;") and len(rows) == 1 + 7 + 1 + 1
    cats = [r.split(";", 1)[0] for r in rows[1:]]
    assert cats == sorted(cats, key=["books", "notes", "audio", "video", "archives"].index)
