import http.client
import os
import threading

from azbyka_reserv.catalog import build_catalog
from azbyka_reserv.crawler import Crawler
from azbyka_reserv.search import build_index, search
from azbyka_reserv.serve import make_server

from .mock_site import MP3, Site
from .test_crawl import make_cfg


def test_viewer_search_catalog(tmp_path):
    with Site() as site:
        data = str(tmp_path / "data")
        c = Crawler(make_cfg(site), data)
        c.run(progress_every=0.3, max_seconds=60)
        c.store.close()
        base = site.base

    build_index(data)
    total, hits = search(os.path.join(data, "search.sqlite"), "москв")
    assert total >= 1 and any("palomnik" in h["url"] for h in hits)

    cat = build_catalog(data)
    with open(cat, encoding="utf-8") as f:
        assert "Книги" in f.read()
    with open(os.path.join(data, "catalog", "books-1.html"), encoding="utf-8") as f:
        assert "Книга.epub" in f.read()
    with open(os.path.join(data, "catalog", "audio-1.html"), encoding="utf-8") as f:
        assert "t1.mp3" in f.read()

    srv = make_server(data, "127.0.0.1", 0, main_origin=base)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        def get(path, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            body = r.read()
            conn.close()
            return r, body

        r, body = get("/")
        assert r.status == 200 and "Главная".encode() in body and b"/__azr__/search" in body
        r, _ = get("/redir")
        assert r.status == 302 and r.getheader("Location") == "/a"
        r, body = get("/files/t1.mp3", {"Range": "bytes=100-199"})
        assert r.status == 206 and body == MP3[100:200]
        r, body = get("/palomnik/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0")
        assert r.status == 200
        r, body = get("/palomnik/%d0%9c%d0%be%d1%81%d0%ba%d0%b2%d0%b0")  # другой регистр %-кодов
        assert r.status == 200
        r, body = get("/net-takoj-stranicy")
        assert r.status == 404
        r, body = get("/__azr__/search?q=%D0%BC%D0%BE%D1%81%D0%BA%D0%B2%D0%B0")
        assert r.status == 200 and "Москва".encode() in body
        r, body = get("/__azr__/")
        assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_rewrite():
    from azbyka_reserv.serve import Archive

    class A(Archive):
        def __init__(self):
            import re
            self.main_host = "azbyka.ru"
            self.aliases = {"azbyka.org": "azbyka.ru"}
            hosts = ["azbyka.ru", "azbyka.org", "cdn.example.com"]
            alt = "|".join(re.escape(h) for h in hosts)
            self._host_rx = re.compile(r"(?:https?:)?(\\?/\\?/)(" + alt + r")(?=[/\\\"'?#\s<)]|$)", re.I)

    a = A()
    assert a.rewrite('<a href="https://azbyka.ru/otechnik/">') == '<a href="/otechnik/">'
    assert a.rewrite('<a href="https://azbyka.org">') == '<a href="/">'
    assert a.rewrite('{"src":"https:\\/\\/azbyka.ru\\/audio\\/x.mp3"}') == '{"src":"\\/audio\\/x.mp3"}'
    assert a.rewrite('<img src="//cdn.example.com/i.png">') == '<img src="/__ext__/cdn.example.com/i.png">'
    assert a.rewrite('<a href="https://notazbyka.ru/x">') == '<a href="https://notazbyka.ru/x">'
