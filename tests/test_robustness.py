"""Устойчивость обходчика: обрывы, докачка, мёртвые хосты, блокировка, relink."""

import os
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from azbyka_reserv.config import Config
from azbyka_reserv.crawler import Crawler
from azbyka_reserv.fetcher import _write_part_meta
from azbyka_reserv.fsutil import acquire_lock

from .test_crawl import make_cfg

DATA = bytes(range(256)) * 400  # 100 КБ


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Srv:
    """Мини-сервер с настраиваемыми ответами: routes[path] = callable(handler)."""

    def __init__(self):
        self.routes = {}
        self.log = []
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                srv.log.append((path, dict(self.headers)))
                fn = srv.routes.get(path)
                if fn is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                fn(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def send(h, code, ctype, body, extra=None, length=None):
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body) if length is None else length))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


class _Site:  # совместимость с make_cfg
    def __init__(self, base):
        self.base = base


def cfg_for(srv, **crawl):
    return make_cfg(_Site(srv.base), **crawl)


def rows(data):
    conn = sqlite3.connect(os.path.join(data, "state.sqlite"))
    conn.row_factory = sqlite3.Row
    return {r["url"]: dict(r) for r in conn.execute("SELECT * FROM urls")}


def test_truncated_page_is_not_saved_as_done(tmp_path):
    srv = Srv()
    calls = {"n": 0}

    def trunc(h):
        calls["n"] += 1
        body = b"<html><title>T</title>" + b"x" * 1000 + b"</html>"
        if calls["n"] == 1:
            # обещаем больше, чем отдаём, и рвём соединение
            send(h, 200, "text/html", body[:100], length=len(body))
            h.close_connection = True
        else:
            send(h, 200, "text/html", body)

    srv.routes["/"] = lambda h: send(h, 200, "text/html", b"<a href='/t'>t</a>")
    srv.routes["/robots.txt"] = lambda h: send(h, 404, "text/plain", b"")
    srv.routes["/t"] = trunc
    try:
        data = str(tmp_path / "d")
        c = Crawler(cfg_for(srv), data)
        c.run(progress_every=0.3, max_seconds=30)
        r = rows(data)
        t = r[srv.base + "/t"]
        assert t["status"] == "done" and t["size"] > 1000 and calls["n"] >= 2
    finally:
        srv.close()


def test_if_range_resume_and_changed_file(tmp_path):
    srv = Srv()
    state = {"etag": '"v2"'}

    def media(h):
        rng = h.headers.get("Range")
        ifr = h.headers.get("If-Range")
        if rng and ifr == state["etag"]:
            start = int(rng.split("=")[1].split("-")[0])
            send(h, 206, "audio/mpeg", DATA[start:],
                 {"Content-Range": f"bytes {start}-{len(DATA)-1}/{len(DATA)}", "ETag": state["etag"]})
        else:
            send(h, 200, "audio/mpeg", DATA, {"ETag": state["etag"]})

    srv.routes["/"] = lambda h: send(h, 200, "text/html", b"<a href='/a.mp3'>a</a>")
    srv.routes["/robots.txt"] = lambda h: send(h, 404, "text/plain", b"")
    srv.routes["/a.mp3"] = media
    try:
        data = str(tmp_path / "d")
        c = Crawler(cfg_for(srv), data)
        part = c._partial_path(srv.base + "/a.mp3")
        # старый недокачанный кусок другой версии файла (etag v1) — должен быть выброшен
        with open(part, "wb") as f:
            f.write(b"OLD" * 1000)
        _write_part_meta(part, {"etag": '"v1"'})
        c.run(progress_every=0.3, max_seconds=30)
        r = rows(data)
        row = r[srv.base + "/a.mp3"]
        with open(os.path.join(data, "mirror", *row["path"].split("/")), "rb") as f:
            assert f.read() == DATA  # без «склейки» старого и нового
    finally:
        srv.close()


def test_dead_external_host_does_not_livelock(tmp_path):
    srv = Srv()
    dead = f"http://127.0.0.1:{_free_port()}"  # никто не слушает
    srv.routes["/"] = lambda h: send(h, 200, "text/html",
                                     f"<img src='{dead}/x.png'><img src='{dead}/y.png'><a href='/ok'>ok</a>".encode())
    srv.routes["/ok"] = lambda h: send(h, 200, "text/html", b"<title>ok</title>")
    srv.routes["/robots.txt"] = lambda h: send(h, 404, "text/plain", b"")
    try:
        data = str(tmp_path / "d")
        c = Crawler(cfg_for(srv), data)
        c.run(progress_every=0.3, max_seconds=40)
        assert c.stop_reason.startswith("очередь пуста"), c.stop_reason
        r = rows(data)
        assert r[srv.base + "/ok"]["status"] == "done"
        assert r[dead + "/x.png"]["status"] == "error"  # попытки тратятся, обход завершается
    finally:
        srv.close()


def test_single_instance_lock(tmp_path):
    d = str(tmp_path / "d")
    a = acquire_lock(d)
    assert a is not None
    # второй захват в другом процессе невозможен; в том же процессе flock на новом fd тоже не даётся
    import subprocess
    import sys

    code = ("import sys; from azbyka_reserv.fsutil import acquire_lock; "
            f"sys.exit(0 if acquire_lock({d!r}) is None else 1)")
    assert subprocess.run([sys.executable, "-c", code], cwd=os.getcwd()).returncode == 0


def test_luke_and_icons_not_excluded():
    from azbyka_reserv.config import load_config

    cfg = load_config()
    assert not cfg.excluded("https://media.azbyka.ru/audio/biblia/r/Lk/1.mp3")
    assert not cfg.excluded("https://azbyka.ru/audio/audio1/zachala/Lk/Lk.3:19-22.mp3")
    assert not cfg.date_filtered("https://azbyka.ru/days/storage/images/icons-of-saints/1851/p.png")
    assert cfg.excluded("https://azbyka.ru/auth/?reflink=x")
    assert cfg.excluded("https://azbyka.ru/zdorovie/forum/threads/x.1/")


def test_relink_resume(tmp_path):
    srv = Srv()
    links = "".join(f"<a href='/p{i}'>p</a>" for i in range(30))
    srv.routes["/"] = lambda h: send(h, 200, "text/html", links.encode())
    for i in range(30):
        srv.routes[f"/p{i}"] = (lambda i: (lambda h: send(h, 200, "text/html", f"<a href='/q{i}'>q</a>".encode())))(i)
    srv.routes["/robots.txt"] = lambda h: send(h, 404, "text/plain", b"")
    try:
        data = str(tmp_path / "d")
        cfg = cfg_for(srv)
        cfg.raw["rules"]["exclude"] = [r"/q\d+$"]
        c = Crawler(Config(cfg.raw), data)
        c.run(progress_every=0.3, max_seconds=30)
        c.store.close()
        r = rows(data)
        assert not any("/q" in u for u in r)
        cfg.raw["rules"]["exclude"] = []
        c2 = Crawler(Config(cfg.raw), data)
        c2.store.set_meta("relink_pos", "0")
        n = c2.relink(resume_key="relink_pos")
        assert n == 30
        assert c2.store.get_meta("relink_pos") is None  # завершён — позиция очищена
    finally:
        srv.close()
