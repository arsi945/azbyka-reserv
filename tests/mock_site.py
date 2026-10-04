"""Маленький поддельный «сайт» для интеграционных тестов обходчика."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MP3 = bytes(range(256)) * 1200  # ~300 КБ
EPUB = b"PK\x03\x04" + b"epub" * 1000


class Site:
    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass

    def hit(self, path: str) -> int:
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1
            return self.hits[path]

    def pages(self) -> dict:
        b = self.base
        return {
            "/robots.txt": ("text/plain", f"User-agent: *\nDisallow: /private/\nClean-param: sid /\nSitemap: {b}/sitemap.xml\n"),
            "/sitemap.xml": ("application/xml", f"<urlset><url><loc>{b}/from-sitemap</loc><image:image><image:loc>{b}/img/sm.jpg</image:loc></image:image></url></urlset>"),
            "/img/sm.jpg": ("image/jpeg", b"jpg"),
            "/from-sitemap": ("text/html", "<html><title>SM</title>ok</html>"),
            "/": ("text/html", f"""<html><head><title>Главная</title><link rel=stylesheet href="/s.css"></head><body>
                <a href="/a">a</a> <a href="/b/">b</a> <a href="/forum/x">forum</a> <a href="/private/y">priv</a>
                <a href="/days/2026-01-01">day</a> <a href="/days/1990-01-01">old day</a>
                <a href="/redir">redir</a> <a href="/a?utm_source=zz">dup</a> <a href="/a?sid=123">clean</a>
                <a href="/q?x=1">q1</a><a href="/q?x=2">q2</a><a href="/q?x=3">q3</a><a href="/q?x=4">q4</a>
                <a href="/palomnik/Москва">ru</a> <a href="/palomnik/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0">ru2</a>
                <a href="/download/book">book</a> <a href="/protected.pdf">pdf</a> <a href="/flaky">flaky</a>
                <a href="/audio/book/">audio</a> <a href="/gone">gone</a>
                <img src="/img/x.png"><iframe src="https://www.youtube.com/embed/abc123"></iframe>
                <a href="https://other.example.com/page">ext page</a>
                </body></html>"""),
            "/s.css": ("text/css", "body{background:url(/img/bg.png)}"),
            "/img/x.png": ("image/png", b"\x89PNG....x"),
            "/img/bg.png": ("image/png", b"\x89PNG....bg"),
            "/a": ("text/html", "<html><title>A</title><a href='/'>home</a><a href='/b/'>b</a></html>"),
            "/b/": ("text/html", "<html><title>B</title><a href='../a'>a</a></html>"),
            "/days/2026-01-01": ("text/html", "<html><title>День</title><a href='/days/2026-01-02'>next</a></html>"),
            "/days/2026-01-02": ("text/html", "<html><title>День 2</title></html>"),
            "/q": ("text/html", "<html>q</html>"),
            "/palomnik/Москва": ("text/html", "<html><title>Москва</title></html>"),
            "/audio/book/": ("text/html", """<html><title>Аудиокнига</title>
                <script class="wp-playlist-script" type="application/json">{"tracks":[{"src":"\\/files\\/t1.mp3","title":"1"}]}</script>
                <a href="/files/book.m3u">m3u</a></html>"""),
            "/files/book.m3u": ("audio/x-mpegurl", f"#EXTM3U\n{b}/files/t2.mp3\n"),
        }

    def _handler(self):
        site = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body, extra=None):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                from urllib.parse import unquote

                raw = self.path
                path = unquote(raw.split("?", 1)[0])
                n = site.hit(raw)
                pages = site.pages()
                if path == "/redir":
                    return self._send(301, "text/html", "", {"Location": "/a"})
                if path == "/flaky":
                    if n == 1:
                        return self._send(503, "text/html", "busy", {"Retry-After": "0"})
                    return self._send(200, "text/html", "<html><title>Flaky</title></html>")
                if path == "/download/book":
                    return self._send(200, "application/epub+zip", EPUB,
                                      {"Content-Disposition": "attachment; filename*=UTF-8''%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0.epub"})
                if path == "/protected.pdf":
                    return self._send(200, "text/html; charset=utf-8", '<form><input type="password" name="pwd"></form>')
                if path in ("/files/t1.mp3", "/files/t2.mp3"):
                    rng = self.headers.get("Range")
                    if rng:
                        start = int(rng.split("=")[1].split("-")[0])
                        body = MP3[start:]
                        return self._send(206, "audio/mpeg", body,
                                          {"Content-Range": f"bytes {start}-{len(MP3)-1}/{len(MP3)}", "Accept-Ranges": "bytes"})
                    return self._send(200, "audio/mpeg", MP3, {"Accept-Ranges": "bytes"})
                if path in pages:
                    ct, body = pages[path]
                    return self._send(200, ct, body)
                return self._send(404, "text/html", "nf")

        return H
