"""Офлайн-просмотр архива в браузере.

Запросы вида ``/otechnik/...`` отображаются в сохранённые страницы
``https://azbyka.ru/otechnik/...``. Абсолютные ссылки на сайт внутри
страниц переписываются «на лету», файлы на диске не меняются.
Поддерживаются Range-запросы (перемотка аудио/видео) и поиск,
если построен индекс (команда ``index``).
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .extract import sniff_charset
from .fsutil import human_bytes, mirror_file
from .urls import UrlRules

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/css", "application/javascript", "text/javascript",
              "application/x-javascript", "application/json")
SPECIAL = "/__azr__"
EXT_PREFIX = "/__ext__/"


def _base(ctype: str | None) -> str:
    return (ctype or "").split(";", 1)[0].strip().lower()


class Archive:
    def __init__(self, data_dir: str, main_origin: str = "https://azbyka.ru", aliases: dict[str, str] | None = None,
                 drop_params: list[str] | None = None) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.mirror = os.path.join(self.data_dir, "mirror")
        self.db_path = os.path.join(self.data_dir, "state.sqlite")
        self.search_path = os.path.join(self.data_dir, "search.sqlite")
        mo = urllib.parse.urlsplit(main_origin)
        self.main_scheme = mo.scheme
        self.main_host = mo.netloc.lower()
        if aliases is None or drop_params is None:
            from .config import load_config

            cfg = load_config()
            aliases = cfg.host_aliases if aliases is None else aliases
            drop_params = cfg.drop_params if drop_params is None else drop_params
        self.aliases = {k.lower(): v.lower() for k, v in aliases.items()}
        self.peertube_hosts = ["tube.azbyka.ru"]
        self.peertube_max_height = 720
        # те же правила нормализации, что и при обходе (иначе ?ver=… не найдётся)
        self.rules = UrlRules(scope_hosts=[mo.hostname or ""], host_aliases=self.aliases, drop_params=drop_params,
                              https_hosts=[mo.hostname or ""] if mo.scheme == "https" else [])
        self._local = threading.local()
        hosts = set()
        for (h,) in self.db().execute(
            "SELECT DISTINCT substr(url, instr(url,'//')+2, instr(substr(url, instr(url,'//')+2) || '/', '/')-1) FROM urls"
        ):
            if h:
                hosts.add(h.lower())
        hosts |= set(self.aliases) | {self.main_host}
        self.hosts = hosts
        alt = "|".join(sorted((re.escape(h) for h in hosts), key=len, reverse=True))
        self._host_rx = re.compile(
            r"(?:https?:)?(\\?/\\?/)(" + alt + r")(?=[/\\\"'?#\s<)]|$)", re.I
        )

    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "db", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.db = c
        return c

    # -- адресация ------------------------------------------------------------
    def local_to_url(self, raw_path: str) -> str:
        """'/otechnik/x?y' -> 'https://azbyka.ru/otechnik/x?y'; '/__ext__/h/p' -> 'https://h/p'."""
        if raw_path.startswith(EXT_PREFIX):
            rest = raw_path[len(EXT_PREFIX):]
            return "https://" + rest
        return f"{self.main_scheme}://{self.main_host}{raw_path}"

    def rewrite(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            slashes, host = m.group(1), m.group(2).lower()
            canon = self.aliases.get(host, host)
            esc = "\\" in slashes
            nxt = m.string[m.end() : m.end() + 1]
            tail = "" if nxt in ("/", "\\") else "/"
            if canon == self.main_host:
                return tail
            prefix = EXT_PREFIX + host + tail
            return prefix.replace("/", "\\/") if esc else prefix

        return self._host_rx.sub(repl, text)

    def lookup(self, url: str) -> sqlite3.Row | None:
        norm = self.rules.normalize(url)
        cands = [norm[0]] if norm else []
        cands.append(url)
        if norm:
            u = norm[0]
            p = urllib.parse.urlsplit(u)
            if p.path.endswith("/") and len(p.path) > 1:
                cands.append(urllib.parse.urlunsplit(p._replace(path=p.path.rstrip("/"))))
            elif not p.path.endswith("/"):
                cands.append(urllib.parse.urlunsplit(p._replace(path=p.path + "/")))
            if p.scheme == "https":
                cands.append(urllib.parse.urlunsplit(p._replace(scheme="http")))
        db = self.db()
        for c in cands:
            row = db.execute("SELECT * FROM urls WHERE url=?", (c,)).fetchone()
            if row is not None and row["status"] in ("done", "redirect"):
                return row
        return None

    def url_to_local(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        host = p.netloc.lower()
        canon = self.aliases.get(host, host)
        tail = p.path + (("?" + p.query) if p.query else "")
        if canon == self.main_host:
            return tail
        return EXT_PREFIX + host + tail


class Handler(BaseHTTPRequestHandler):
    archive: Archive
    protocol_version = "HTTP/1.1"
    server_version = "azbyka-reserv"

    def log_message(self, fmt, *args):  # тише
        pass

    def _send_bytes(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _page(self, code: int, title: str, body_html: str) -> None:
        doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 16px;color:#222;background:#fff}}
a{{color:#7a2a00}} .r{{margin:1em 0}} .s{{color:#555;font-size:14px}} input{{font-size:16px;padding:6px;width:70%}}
mark{{background:#ffe08a}} table{{border-collapse:collapse}} td{{padding:2px 10px}}</style></head>
<body><p><a href="/">Главная</a> · <a href="{SPECIAL}/">Об архиве</a> · <a href="{SPECIAL}/search">Поиск</a></p>{body_html}</body></html>"""
        self._send_bytes(code, "text/html; charset=utf-8", doc.encode("utf-8"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):  # noqa: C901
        a = self.archive
        path = self.path
        if path.startswith(SPECIAL):
            return self._special(path)
        url = a.local_to_url(path)
        player = self._peertube_player(url)
        if player is not None:
            return self._send_bytes(200, "text/html; charset=utf-8", player.encode("utf-8"))
        row = a.lookup(url)
        if row is None:
            orig = html.escape(url)
            q = html.escape(urllib.parse.unquote(urllib.parse.urlsplit(url).path.strip("/").split("/")[-1]).replace("-", " "))
            return self._page(404, "Нет в архиве",
                              f"<h1>Этой страницы нет в архиве</h1><p class=s>{orig}</p>"
                              f"<p><a href='{SPECIAL}/search?q={urllib.parse.quote(q)}'>Искать похожее в архиве</a></p>")
        if row["status"] == "redirect":
            loc = row["location"]
            target = a.url_to_local(loc) if loc else "/"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fpath = mirror_file(a.mirror, row["path"])
        ctype = row["content_type"] or "application/octet-stream"
        bct = _base(ctype)
        try:
            size = os.path.getsize(fpath)
        except OSError:
            return self._page(404, "Файл потерян", "<h1>Файл отсутствует на диске</h1><p>Запустите <code>verify --fix</code> и <code>crawl</code>.</p>")
        if bct in TEXT_TYPES and size < 50 * 1024 * 1024:
            with open(fpath, "rb") as f:
                raw = f.read()
            cs = sniff_charset(raw, ctype)
            try:
                text = raw.decode(cs, errors="replace")
            except LookupError:
                cs = "utf-8"
                text = raw.decode(cs, errors="replace")
            text = a.rewrite(text)
            if bct in ("text/html", "application/xhtml+xml"):
                badge = (f'<a href="{SPECIAL}/search" style="position:fixed;right:12px;bottom:12px;z-index:99999;'
                         'background:#7a2a00;color:#fff;padding:6px 10px;border-radius:6px;font:14px sans-serif;'
                         'text-decoration:none;opacity:.85">Офлайн-поиск</a>')
                idx = text.lower().rfind("</body>")
                text = text[:idx] + badge + text[idx:] if idx >= 0 else text + badge
            try:
                body = text.encode(cs, errors="xmlcharrefreplace")
            except LookupError:
                body = text.encode("utf-8")
            return self._send_bytes(200, ctype, body, {"Cache-Control": "no-cache"})
        return self._send_file(fpath, ctype, size, row["path"])

    def _peertube_player(self, url: str) -> str | None:
        """Вместо iframe-плеера PeerTube — простой HTML5-плеер со скачанным файлом."""
        from . import peertube

        a = self.archive
        p = urllib.parse.urlsplit(url)
        if not any(p.hostname == h for h in a.peertube_hosts):
            return None
        vid = peertube.video_id(p.path)
        if not vid or p.path.startswith("/api/"):
            return None
        api = a.lookup(peertube.api_url(f"https://{p.netloc}", vid))
        media_row = None
        title = "Видео"
        if api is not None and api["path"]:
            title = api["title"] or title
            media_row = a.db().execute(
                "SELECT * FROM urls WHERE parent_id=? AND kind='media' AND status='done' LIMIT 1", (api["id"],)
            ).fetchone()
            if media_row is None:
                try:
                    import json

                    with open(mirror_file(a.mirror, api["path"]), "rb") as f:
                        data = json.loads(f.read().decode("utf-8", "replace"))
                    title = data.get("name") or title
                    chosen = peertube.choose_file(data, a.peertube_max_height)
                    if chosen:
                        media_row = a.lookup(chosen.get("fileDownloadUrl") or chosen.get("fileUrl"))
                except (OSError, ValueError):
                    pass
        if media_row is None:
            body = "<p style='color:#fff;font:16px sans-serif;padding:1em'>Это видео ещё не скачано в архив.</p>"
        else:
            src = html.escape(a.url_to_local(media_row["url"]))
            body = f"<video controls preload=metadata style='width:100%;height:100%' src='{src}'></video>"
        return (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(title)}</title></head>"
                f"<body style='margin:0;background:#000;height:100vh'>{body}</body></html>")

    def _send_file(self, fpath: str, ctype: str, size: int, relpath: str) -> None:
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        name = relpath.rsplit("/", 1)[-1]
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fpath, "rb") as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- служебные страницы -----------------------------------------------------
    def _special(self, path: str) -> None:
        parts = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(parts.query)
        sub = parts.path[len(SPECIAL):].strip("/")
        if sub == "search":
            return self._search(qs.get("q", [""])[0], int(qs.get("p", ["0"])[0] or 0))
        return self._about()

    def _about(self) -> None:
        db = self.archive.db()
        rows = db.execute("SELECT status, COUNT(*) n, COALESCE(SUM(size),0) b FROM urls GROUP BY status").fetchall()
        stat = "".join(f"<tr><td>{html.escape(r['status'])}</td><td>{r['n']}</td><td>{human_bytes(r['b'])}</td></tr>" for r in rows)
        sections = [
            ("/", "Главная"), ("/biblia/", "Библия"), ("/molitvoslov/", "Молитвослов"), ("/days/", "Календарь"),
            ("/worships/", "Богослужения"), ("/otechnik/", "Библиотека «Отечник»"), ("/fiction/", "Художественная литература"),
            ("/pravo/", "Церковное право"), ("/audio/", "Аудио"), ("/video/", "Видео"), ("/kliros/", "Ноты"),
            ("/art/", "Азбука искусства"), ("/palomnik/", "Азбука паломника"), ("/vopros/", "Вопросы и ответы"),
            ("/quotes/", "Цитаты"), ("/shemy/", "Схемы"), ("/deti/", "Детям"), ("/recept/", "Рецепты"),
        ]
        links = " · ".join(f"<a href='{u}'>{t}</a>" for u, t in sections)
        has_idx = os.path.exists(self.archive.search_path)
        self._page(200, "Офлайн-архив «Азбука веры»",
                   f"<h1>Офлайн-архив «Азбука веры»</h1><p>{links}</p>"
                   f"<form action='{SPECIAL}/search'><input name=q placeholder='Поиск по архиву'> <button>Найти</button></form>"
                   + ("" if has_idx else "<p class=s>Поиск ещё не построен: выполните команду <code>index</code>.</p>")
                   + f"<h2>Состояние</h2><table>{stat}</table>"
                   f"<p class=s>Каталог файлов (книги, аудио, ноты) — папка <code>catalog</code> рядом с архивом.</p>")

    def _search(self, q: str, page: int) -> None:
        a = self.archive
        form = (f"<form action='{SPECIAL}/search'><input name=q value='{html.escape(q)}' autofocus> "
                "<button>Найти</button></form>")
        if not q.strip():
            return self._page(200, "Поиск", "<h1>Поиск по архиву</h1>" + form)
        if not os.path.exists(a.search_path):
            return self._page(200, "Поиск", "<h1>Поиск</h1>" + form + "<p>Индекс не построен: выполните команду <code>index</code>.</p>")
        from .search import search

        per = 30
        try:
            total, hits = search(a.search_path, q, limit=per, offset=page * per)
        except sqlite3.Error as e:
            return self._page(200, "Поиск", "<h1>Поиск</h1>" + form + f"<p>Ошибка запроса: {html.escape(str(e))}</p>")
        out = [f"<h1>Поиск: {html.escape(q)}</h1>", form, f"<p class=s>Найдено: {total}</p>"]
        for h in hits:
            local = a.url_to_local(h["url"])
            out.append(f"<div class=r><a href='{html.escape(local)}'>{html.escape(h['title'] or h['url'])}</a>"
                       f"<div class=s>{h['snippet']}</div><div class=s>{html.escape(urllib.parse.unquote(h['url']))}</div></div>")
        nav = []
        if page > 0:
            nav.append(f"<a href='{SPECIAL}/search?q={urllib.parse.quote(q)}&p={page-1}'>← назад</a>")
        if (page + 1) * per < total:
            nav.append(f"<a href='{SPECIAL}/search?q={urllib.parse.quote(q)}&p={page+1}'>дальше →</a>")
        out.append("<p>" + " · ".join(nav) + "</p>")
        self._page(200, "Поиск", "".join(out))


def make_server(data_dir: str, host: str = "127.0.0.1", port: int = 8080,
                main_origin: str = "https://azbyka.ru", cfg=None) -> ThreadingHTTPServer:
    archive = Archive(data_dir, main_origin,
                      aliases=cfg.host_aliases if cfg else None, drop_params=cfg.drop_params if cfg else None)
    if cfg is not None:
        archive.peertube_hosts = list(cfg.peertube_hosts)
        archive.peertube_max_height = cfg.peertube_max_height
    handler = type("H", (Handler,), {"archive": archive})
    return ThreadingHTTPServer((host, port), handler)


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8080, cfg=None) -> None:
    httpd = make_server(data_dir, host, port, cfg=cfg)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Архив открыт: http://{shown}:{port}/  (служебная страница: http://{shown}:{port}{SPECIAL}/)")
    if host == "0.0.0.0":
        print("Доступен и с других устройств в этой сети по IP этого компьютера.")
    print("Остановить: Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
