"""Офлайн-просмотр архива в браузере.

Запросы вида ``/otechnik/...`` отображаются в сохранённые страницы
``https://azbyka.ru/otechnik/...``. Абсолютные ссылки на сайт внутри
страниц переписываются «на лету», файлы на диске не меняются.
Поддерживаются Range-запросы (перемотка аудио/видео) и поиск,
если построен индекс (команда ``index``).
"""

from __future__ import annotations

import html
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .extract import sniff_charset
from .fsutil import fs_path, human_bytes, mirror_file
from .search import COUNT_CAP, find_ci, sqlite_uri
from .urls import UrlRules

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/css", "application/javascript", "text/javascript",
              "application/x-javascript", "application/json")
PLAYLIST_TYPES = ("audio/x-mpegurl", "audio/mpegurl", "application/vnd.apple.mpegurl", "application/x-mpegurl",
                  "audio/x-scpls")
PLAYLIST_EXT = (".m3u", ".m3u8", ".pls")
SPECIAL = "/__azr__"
VIDEO_ROUTE = SPECIAL + "/video/"
_THUMB_RX = re.compile(r"-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp|gif))(?=\?|$)", re.I)
EXT_PREFIX = "/__ext__/"
DEFAULT_EMBED_HOSTS = ["youtube.com", "youtube-nocookie.com", "youtu.be", "rutube.ru", "vk.com", "vkvideo.ru",
                       "vimeo.com", "ok.ru", "dzen.ru"]
# src у <iframe>/<embed> — сторонние плееры переводятся на локальную заглушку
_IFRAME_RX = re.compile(
    r"""(<(?:iframe|embed)\b[^>]*?\s(?:src|data-src|data-lazy-src)\s*=\s*["']?)(?:https?:)?//([a-z0-9][a-z0-9.-]*)""",
    re.I,
)
_HLS_URI_RX = re.compile(r'(URI=")([^"]+)(")', re.I)
_HOST_HDR_RX = re.compile(r"[A-Za-z0-9.\-]+(?::\d{1,5})?|\[[0-9A-Fa-f:.]+\](?::\d{1,5})?")
_RANGE_RX = re.compile(r"bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.I)
_BADGE_TEXT = "".join(c if ord(c) < 128 else f"&#{ord(c)};" for c in "Офлайн-поиск")
MAX_PAGE = 10_000


def _base(ctype: str | None) -> str:
    return (ctype or "").split(";", 1)[0].strip().lower()


def _host_matches(host: str, patterns) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == p or host.endswith("." + p) for p in patterns)


def _video_key(url: str) -> str:
    """Ключ сравнения адресов видео: канонический вид без схемы, www. и m."""
    from .video import canonical_video_url

    p = urllib.parse.urlsplit(canonical_video_url(url))
    host = (p.hostname or "").lower()
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    return host + p.path.rstrip("/") + ("?" + p.query if p.query else "")


def safe_video_path(data_dir: str, rel: str) -> str | None:
    """Путь к файлу ``data/video/...`` или None, если ``rel`` выходит за пределы папки video."""
    if not rel or "\x00" in rel or "\\" in rel:
        return None
    segs = rel.split("/")
    if len(segs) < 2 or segs[0] != "video":
        return None
    if any(s in ("", ".", "..") or ":" in s for s in segs):
        return None
    base = os.path.realpath(os.path.join(data_dir, "video"))
    full = os.path.realpath(os.path.join(data_dir, *segs))
    try:
        if full == base or os.path.commonpath([base, full]) != base:
            return None
    except ValueError:  # разные диски Windows
        return None
    return full


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
        from .config import load_config

        self._cfg = load_config()
        if aliases is None:
            aliases = self._cfg.host_aliases
        if drop_params is None:
            drop_params = self._cfg.drop_params
        self.aliases = {k.lower(): v.lower() for k, v in aliases.items()}
        self.peertube_hosts = ["tube.azbyka.ru"]
        self.peertube_max_height = 720
        # те же правила нормализации, что и при обходе (иначе ?ver=… не найдётся)
        self.rules = UrlRules(scope_hosts=[mo.hostname or ""], host_aliases=self.aliases, drop_params=drop_params,
                              https_hosts=[mo.hostname or ""] if mo.scheme == "https" else [])
        self._local = threading.local()
        self._embed_lock = threading.Lock()
        self._embed_map: dict[str, str] = {}
        self._embed_map_at = 0.0
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

    @property
    def embed_hosts(self) -> list[str]:
        hosts = getattr(getattr(self, "_cfg", None), "embed_hosts", None)
        return [h.lower() for h in hosts] if hosts else DEFAULT_EMBED_HOSTS

    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "db", None)
        if c is None:
            c = sqlite3.connect(sqlite_uri(self.db_path), uri=True, timeout=30, check_same_thread=False)
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

        text = self._host_rx.sub(repl, text)
        embed_hosts = self.embed_hosts

        def iframe(m: re.Match) -> str:
            host = m.group(2).lower()
            if _host_matches(host, embed_hosts):
                return m.group(1) + EXT_PREFIX + host
            return m.group(0)

        return _IFRAME_RX.sub(iframe, text)

    def _candidates(self, url: str) -> list[str]:
        cands: list[str] = []

        def add(u: str | None) -> None:
            if u and u not in cands:
                cands.append(u)

        norm = self.rules.normalize(url)
        add(norm[0] if norm else None)
        add(url)
        if norm:
            u = norm[0]
            p = urllib.parse.urlsplit(u)
            if p.path.endswith("/") and len(p.path) > 1:
                add(urllib.parse.urlunsplit(p._replace(path=p.path.rstrip("/"))))
            elif not p.path.endswith("/"):
                add(urllib.parse.urlunsplit(p._replace(path=p.path + "/")))
            if p.scheme == "https":
                add(urllib.parse.urlunsplit(p._replace(scheme="http")))
            rw = self._cfg.rewrite(u)  # например, стих Библии -> страница главы
            if rw != u:
                add(rw)
            # уменьшенные копии картинок WordPress не качаются — отдать оригинал
            orig = _THUMB_RX.sub(r"\1", u)
            if orig != u:
                add(orig)
        # браузеры кодируют ' в запросе как %27, а в базе может быть «сырая» кавычка (и наоборот)
        for c in list(cands):
            if "%27" in c:
                add(c.replace("%27", "'"))
            elif "'" in c:
                add(c.replace("'", "%27"))
        return cands

    def lookup(self, url: str, statuses: tuple[str, ...] = ("done", "redirect")) -> sqlite3.Row | None:
        """Строка urls для адреса. Сначала ищется скачанная ('done') среди всех
        вариантов адреса и только потом — редирект: иначе /x -> /x/ -> /x… зацикливается."""
        db = self.db()
        found = []
        for c in self._candidates(url):
            row = db.execute("SELECT * FROM urls WHERE url=?", (c,)).fetchone()
            if row is None or row["status"] not in statuses:
                continue
            if row["status"] == statuses[0]:
                return row
            found.append(row)
        for st in statuses[1:]:
            for row in found:
                if row["status"] == st:
                    return row
        return None

    def url_key(self, url: str) -> tuple[str, str, str]:
        """Ключ сравнения адресов: без схемы и конечной косой черты."""
        norm = self.rules.normalize(url)
        p = urllib.parse.urlsplit(norm[0] if norm else url)
        return ((p.netloc or "").lower(), p.path.rstrip("/") or "/", p.query)

    def resolve_redirect(self, url: str, row: sqlite3.Row) -> tuple[str, object]:
        """Куда ведёт редирект ``row`` (найденный для ``url``).

        ('redirect', адрес) — отправить браузер туда; ('serve', строка) —
        отдать скачанную страницу сразу (цель совпадает с запрошенным
        адресом); ('missing', None) — цепочка зацикливается и скачанной
        страницы нет.
        """
        start = self.url_key(url)
        seen = {start}
        cur = row
        target: str | None = None
        for _ in range(10):
            loc = cur["location"]
            if not loc:
                return ("redirect", target)
            key = self.url_key(loc)
            if key in seen:
                done = self.lookup(url, ("done",)) or self.lookup(loc, ("done",))
                return ("serve", done) if done is not None else ("missing", None)
            seen.add(key)
            target = loc
            nxt = self.lookup(loc)
            if nxt is None:
                break
            if nxt["status"] == "done":
                if self.url_key(nxt["url"]) == start:
                    return ("serve", nxt)
                break
            cur = nxt
        else:
            return ("missing", None)
        return ("redirect", target)

    def url_to_local(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        host = p.netloc.lower()
        canon = self.aliases.get(host, host)
        tail = p.path + (("?" + p.query) if p.query else "")
        if canon == self.main_host:
            return tail
        return EXT_PREFIX + host + tail

    # -- файлы ---------------------------------------------------------------
    def find_file(self, relpath: str) -> tuple[str, int] | None:
        """(путь, размер) файла зеркала; при неудаче — поиск без учёта регистра имён."""
        fpath = mirror_file(self.mirror, relpath)
        try:
            return fpath, os.path.getsize(fpath)
        except OSError:
            pass
        alt = find_ci(self.mirror, relpath)
        if alt is not None:
            try:
                return alt, os.path.getsize(alt)
            except OSError:
                pass
        return None

    def embed_video(self, url: str) -> str | None:
        """Путь (от папки data) скачанного командой video ролика для адреса плеера."""
        db = self.db()
        cands = [url]
        norm = self.rules.normalize(url)
        if norm and norm[0] not in cands:
            cands.append(norm[0])
        for c in list(cands):
            p = urllib.parse.urlsplit(c)
            other = urllib.parse.urlunsplit(p._replace(scheme="http" if p.scheme == "https" else "https"))
            if other not in cands:
                cands.append(other)
        try:
            for c in cands:
                row = db.execute("SELECT path FROM embeds WHERE url=? AND status='done' AND path IS NOT NULL",
                                 (c,)).fetchone()
                if row is not None:
                    return row[0].replace("\\", "/")
            with self._embed_lock:
                if time.time() - self._embed_map_at > 60:
                    m: dict[str, str] = {}
                    for r in db.execute("SELECT url, path FROM embeds WHERE status='done' AND path IS NOT NULL"):
                        m.setdefault(_video_key(r[0]), r[1].replace("\\", "/"))
                    self._embed_map, self._embed_map_at = m, time.time()
                return self._embed_map.get(_video_key(url))
        except sqlite3.Error:  # старая база без таблицы embeds
            return None


class Handler(BaseHTTPRequestHandler):
    archive: Archive
    protocol_version = "HTTP/1.1"
    server_version = "azbyka-reserv"

    def log_message(self, fmt, *args):  # тише
        pass

    def _send_bytes(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except ConnectionError:  # браузер закрыл соединение (в т.ч. WinError 10053/10054)
            self.close_connection = True

    def _page(self, code: int, title: str, body_html: str) -> None:
        doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 16px;color:#222;background:#fff}}
a{{color:#7a2a00}} .r{{margin:1em 0}} .s{{color:#555;font-size:14px}} input{{font-size:16px;padding:6px;width:70%}}
mark{{background:#ffe08a}} table{{border-collapse:collapse}} td{{padding:2px 10px}}</style></head>
<body><p><a href="/">Главная</a> · <a href="{SPECIAL}/">Об архиве</a> · <a href="{SPECIAL}/search">Поиск</a></p>{body_html}</body></html>"""
        self._send_bytes(code, "text/html; charset=utf-8", doc.encode("utf-8"))

    def _not_found(self, url: str) -> None:
        orig = html.escape(url)
        q = html.escape(urllib.parse.unquote(urllib.parse.urlsplit(url).path.strip("/").split("/")[-1]).replace("-", " "))
        self._page(404, "Нет в архиве",
                   f"<h1>Этой страницы нет в архиве</h1><p class=s>{orig}</p>"
                   f"<p><a href='{SPECIAL}/search?q={urllib.parse.quote(q)}'>Искать похожее в архиве</a></p>")

    def _redirect(self, target: str) -> None:
        try:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ConnectionError:
            self.close_connection = True

    def _origin(self) -> str:
        """http://хост:порт, по которому браузер обратился к серверу (для абсолютных ссылок)."""
        host = (self.headers.get("Host") or "").strip()
        if not _HOST_HDR_RX.fullmatch(host):
            addr, port = self.server.server_address[:2]
            if addr in ("0.0.0.0", "::", ""):
                addr = "127.0.0.1"
            host = f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"
        return "http://" + host

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
        host = urllib.parse.urlsplit(url).hostname or ""
        if _host_matches(host, a.embed_hosts):
            return self._embed(url)
        return self._serve_found(url, a.lookup(url))

    def _serve_found(self, url: str, row: sqlite3.Row | None) -> None:
        a = self.archive
        if row is None:
            return self._not_found(url)
        if row["status"] == "redirect":
            action, value = a.resolve_redirect(url, row)
            if action == "serve":
                row = value
            elif action == "missing":
                return self._not_found(url)
            else:
                # никогда не на самого себя: такие цепочки resolve_redirect разворачивает
                return self._redirect(a.url_to_local(value) if value else "/")
        return self._serve_row(row)

    def _serve_row(self, row: sqlite3.Row) -> None:
        a = self.archive
        found = a.find_file(row["path"]) if row["path"] else None
        if found is None:
            return self._page(404, "Файл потерян", "<h1>Файл отсутствует на диске</h1><p>Запустите <code>verify --fix</code> и <code>crawl</code>.</p>")
        fpath, size = found
        ctype = row["content_type"] or "application/octet-stream"
        bct = _base(ctype)
        is_playlist = bct in PLAYLIST_TYPES or any(
            x.lower().endswith(PLAYLIST_EXT) for x in (urllib.parse.urlsplit(row["url"]).path, row["path"])
        )
        if (bct in TEXT_TYPES or is_playlist) and size < 50 * 1024 * 1024:
            try:
                with open(fpath, "rb") as f:
                    raw = f.read()
            except OSError:
                return self._page(404, "Файл потерян", "<h1>Файл недоступен</h1>")
            cs = sniff_charset(raw, ctype)
            # surrogateescape: байты, не подходящие под кодировку, возвращаются как были
            try:
                text = raw.decode(cs, errors="surrogateescape")
            except LookupError:
                cs = "utf-8"
                text = raw.decode(cs, errors="surrogateescape")
            if is_playlist:
                text = self._rewrite_playlist(text, row["url"], row["path"], bct)
                if bct not in PLAYLIST_TYPES:
                    ext = row["path"].lower().rsplit(".", 1)[-1]
                    ctype = {"m3u8": "application/vnd.apple.mpegurl", "pls": "audio/x-scpls"}.get(ext, "audio/x-mpegurl")
                    if cs.lower().replace("-", "").replace("_", "") != "utf8":
                        ctype += f"; charset={cs}"
            else:
                text = a.rewrite(text)
                if bct in ("text/html", "application/xhtml+xml"):
                    # только ASCII (кириллица — сущностями): страница может быть в любой кодировке
                    badge = (f'<a href="{SPECIAL}/search" style="position:fixed;right:12px;bottom:12px;z-index:99999;'
                             'background:#7a2a00;color:#fff;padding:6px 10px;border-radius:6px;font:14px sans-serif;'
                             f'text-decoration:none;opacity:.85">{_BADGE_TEXT}</a>')
                    idx = text.lower().rfind("</body>")
                    text = text[:idx] + badge + text[idx:] if idx >= 0 else text + badge
            try:
                body = text.encode(cs, errors="surrogateescape")
            except UnicodeError:
                body = text.encode(cs, errors="xmlcharrefreplace")
            return self._send_bytes(200, ctype, body, {"Cache-Control": "no-cache"})
        return self._send_file(fpath, ctype, size, row["path"])

    # -- плейлисты --------------------------------------------------------------
    def _rewrite_playlist(self, text: str, base_url: str, relpath: str, bct: str) -> str:
        """Строки M3U/PLS -> абсолютные адреса этого сервера (для внешних плееров)."""
        a = self.archive
        origin = self._origin()
        pls = bct == "audio/x-scpls" or relpath.lower().endswith(".pls")

        def absolute(ref: str) -> str:
            ref = ref.strip()
            try:
                u = urllib.parse.urljoin(base_url, ref)
                p = urllib.parse.urlsplit(u)
            except ValueError:
                return ref
            if p.scheme not in ("http", "https") or not p.netloc:
                return ref
            norm = a.rules.normalize(u)
            return origin + a.url_to_local(norm[0] if norm else u)

        out = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            eol = line[len(body):]
            s = body.strip()
            bom = "\ufeff" if s.startswith("\ufeff") else ""
            s = s.lstrip("\ufeff")
            if not s:
                out.append(line)
            elif s.startswith("#"):
                # HLS: #EXT-X-KEY:…,URI="key.bin" и т.п.
                out.append(_HLS_URI_RX.sub(lambda m: m.group(1) + absolute(m.group(2)) + m.group(3), body) + eol)
            elif pls:
                k, sep, v = s.partition("=")
                if sep and re.fullmatch(r"(?i)file\d+", k.strip()):
                    out.append(bom + k + "=" + absolute(v) + eol)
                else:
                    out.append(line)
            else:
                out.append(bom + absolute(s) + eol)
        return "".join(out)

    # -- встроенные видео ----------------------------------------------------------
    def _embed(self, url: str) -> None:
        """Плеер YouTube/RuTube/VK… -> ролик, скачанный командой video, или пояснение."""
        a = self.archive
        rel = a.embed_video(url)
        if rel is not None and rel.startswith("video/"):
            # /__azr__/<путь от папки data>, т.е. /__azr__/video/…
            src = html.escape(SPECIAL + "/" + "/".join(urllib.parse.quote(s, safe="") for s in rel.split("/")))
            doc = ("<!doctype html><html lang=ru><head><meta charset=utf-8><title>Видео</title></head>"
                   "<body style='margin:0;background:#000;height:100vh'>"
                   f"<video controls preload=metadata style='width:100%;height:100%' src=\"{src}\"></video></body></html>")
            return self._send_bytes(200, "text/html; charset=utf-8", doc.encode("utf-8"))
        row = a.lookup(url)
        if row is not None:  # что-то с этого хоста всё же скачано обходчиком
            return self._serve_found(url, row)
        doc = ("<!doctype html><html lang=ru><head><meta charset=utf-8><title>Видео не скачано</title></head>"
               "<body style='margin:0;background:#111;color:#eee;font:16px/1.5 system-ui,sans-serif;padding:1em'>"
               "<p>Видео не скачано (команда video).</p>"
               f"<p style='font-size:13px;word-break:break-all'><a style='color:#9cf' href=\"{html.escape(url)}\" "
               f"target=_blank rel=noopener>{html.escape(url)}</a></p></body></html>")
        self._send_bytes(200, "text/html; charset=utf-8", doc.encode("utf-8"))

    def _video_file(self, quoted: str) -> None:
        """/__azr__/video/… -> файл data/video/… (только внутри этой папки)."""
        a = self.archive
        rel = urllib.parse.unquote(quoted)
        full = safe_video_path(a.data_dir, rel)
        if full is None:
            return self._page(404, "Нет файла", "<h1>Недопустимый путь</h1>")
        try:
            size = os.path.getsize(fs_path(full))
        except OSError:
            return self._page(404, "Нет файла", "<h1>Видеофайл не найден</h1>")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._send_file(fs_path(full), ctype, size, rel)

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

                    found = a.find_file(api["path"])
                    if found is None:
                        raise OSError(api["path"])
                    with open(found[0], "rb") as f:
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
        rng = (self.headers.get("Range") or "").strip()
        # несколько диапазонов («bytes=0-1,5-6») не поддерживаем — отдаём файл целиком (RFC 9110 это допускает)
        if rng and "," not in rng:
            m = _RANGE_RX.match(rng)
            if m and (m.group(1) or m.group(2)):
                s, e = m.group(1), m.group(2)
                ok = True
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                    if end < start:
                        ok = False
                else:
                    n = int(e)
                    if n == 0:
                        ok = False
                    start = max(0, size - n)
                if not ok or start >= size:
                    try:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except ConnectionError:
                        self.close_connection = True
                    return
                end = min(end, size - 1)
                code = 206
            # нераспознанный заголовок Range игнорируется — 200 и весь файл
        try:
            f = open(fpath, "rb")
        except OSError:
            return self._page(404, "Файл потерян", "<h1>Файл недоступен</h1>")
        length = end - start + 1
        with f:
            try:
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
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except ConnectionError:  # перемотка/закрытие вкладки: браузер рвёт соединение
                self.close_connection = True

    # -- служебные страницы -----------------------------------------------------
    def _special(self, path: str) -> None:
        if path.startswith(VIDEO_ROUTE):
            return self._video_file(urllib.parse.urlsplit(path).path[len(SPECIAL) + 1:])
        parts = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(parts.query)
        sub = parts.path[len(SPECIAL):].strip("/")
        if sub == "search":
            try:
                page = int((qs.get("p", ["0"])[0] or "0").strip())
            except ValueError:
                page = 0
            return self._search(qs.get("q", [""])[0], min(max(page, 0), MAX_PAGE))
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
        shown = f"{COUNT_CAP}+" if total > COUNT_CAP else str(total)
        out = [f"<h1>Поиск: {html.escape(q)}</h1>", form, f"<p class=s>Найдено: {shown}</p>"]
        for h in hits:
            local = a.url_to_local(h["url"])
            out.append(f"<div class=r><a href='{html.escape(local)}'>{html.escape(h['title'] or h['url'])}</a>"
                       f"<div class=s>{h['snippet']}</div><div class=s>{html.escape(urllib.parse.unquote(h['url']))}</div></div>")
        nav = []
        if page > 0:
            nav.append(f"<a href='{SPECIAL}/search?q={urllib.parse.quote(q)}&p={page-1}'>← назад</a>")
        more = (page + 1) * per < total or (total > COUNT_CAP and len(hits) == per)
        if more and page < MAX_PAGE:
            nav.append(f"<a href='{SPECIAL}/search?q={urllib.parse.quote(q)}&p={page+1}'>дальше →</a>")
        out.append("<p>" + " · ".join(nav) + "</p>")
        self._page(200, "Поиск", "".join(out))


class ArchiveServer(ThreadingHTTPServer):
    """Сервер просмотра: оборванные браузером соединения — не ошибка."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(data_dir: str, host: str = "127.0.0.1", port: int = 8080,
                main_origin: str = "https://azbyka.ru", cfg=None) -> ThreadingHTTPServer:
    archive = Archive(data_dir, main_origin,
                      aliases=cfg.host_aliases if cfg else None, drop_params=cfg.drop_params if cfg else None)
    if cfg is not None:
        archive._cfg = cfg
        archive.peertube_hosts = list(cfg.peertube_hosts)
        archive.peertube_max_height = cfg.peertube_max_height
    handler = type("H", (Handler,), {"archive": archive})
    return ArchiveServer((host, port), handler)


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
