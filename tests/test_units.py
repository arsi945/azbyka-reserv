from azbyka_reserv.config import load_config
from azbyka_reserv.extract import extract_links, html_links, text_links
from azbyka_reserv.robots import Robots
from azbyka_reserv.urls import UrlRules, kind_by_ext, safe_segment, url_to_relpath


def rules():
    return UrlRules(
        scope_hosts=["azbyka.ru"],
        host_aliases={"azbyka.org": "azbyka.ru", "www.azbyka.ru": "azbyka.ru"},
        drop_params=["utm_*", "fbclid", "replytocom"],
        https_hosts=["azbyka.ru"],
    )


def test_normalize_basic():
    r = rules()
    assert r.normalize("http://www.azbyka.ru/otechnik/#top") == ("https://azbyka.ru/otechnik/", "http://www.azbyka.ru/otechnik/")
    assert r.normalize("/a/./b/../c?utm_source=x&id=1", "https://azbyka.ru/x/")[0] == "https://azbyka.ru/a/c?id=1"
    assert r.normalize("mailto:a@b.c") is None
    assert r.normalize("javascript:void(0)") is None
    assert r.normalize("https://azbyka.ru:443/x")[0] == "https://azbyka.ru/x"


def test_normalize_cyrillic_equivalence():
    r = rules()
    a = r.normalize("https://azbyka.ru/palomnik/Москва")[0]
    b = r.normalize("https://azbyka.ru/palomnik/%d0%9c%d0%be%d1%81%d0%ba%d0%b2%d0%b0")[0]
    assert a == b == "https://azbyka.ru/palomnik/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0"


def test_normalize_bible_query_preserved():
    r = rules()
    assert r.normalize("https://azbyka.ru/biblia/?Mt.1:1&c~r&rus")[0] == "https://azbyka.ru/biblia/?Mt.1:1&c~r&rus"
    # %7E (тильда) раскодируется в ~, двоеточие не трогается
    assert r.normalize("https://azbyka.ru/biblia/?Mt.1:1&c%7Er")[0] == "https://azbyka.ru/biblia/?Mt.1:1&c~r"


def test_drop_params_only_own_hosts():
    r = UrlRules(scope_hosts=["azbyka.ru"], drop_params=["v", "ver"], https_hosts=["azbyka.ru"])
    assert r.normalize("https://azbyka.ru/s.css?ver=5.1")[0] == "https://azbyka.ru/s.css"
    assert r.normalize("https://www.youtube.com/watch?v=abc")[0] == "https://www.youtube.com/watch?v=abc"


def test_scope():
    r = rules()
    assert r.in_scope("https://static.azbyka.ru/x.png")
    assert not r.in_scope("https://notazbyka.ru/")
    assert not r.in_scope("https://youtube.com/")


def test_relpath():
    assert url_to_relpath("https://azbyka.ru/", "text/html") == "azbyka.ru/index.html"
    assert url_to_relpath("https://azbyka.ru/drevo-zhizni", "text/html") == "azbyka.ru/drevo-zhizni/index.html"
    assert url_to_relpath("https://azbyka.ru/otechnik/Biblia/", "text/html") == "azbyka.ru/otechnik/Biblia/index.html"
    assert url_to_relpath("https://azbyka.ru/a/b.mp3", "audio/mpeg") == "azbyka.ru/a/b.mp3"
    assert url_to_relpath("https://azbyka.ru/shemy/x.shtml", "text/html") == "azbyka.ru/shemy/x.shtml.html"
    p = url_to_relpath("https://azbyka.ru/biblia/?Mt.1:1", "text/html")
    assert p.startswith("azbyka.ru/biblia/index@") and p.endswith(".html")
    assert url_to_relpath("https://azbyka.ru/d/42", "application/epub+zip", "Книга.epub") == "azbyka.ru/d/42/Книга.epub"
    assert url_to_relpath("https://azbyka.ru/palomnik/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0", "text/html") == "azbyka.ru/palomnik/Москва/index.html"


def test_relpath_windows_safety():
    assert safe_segment("CON") == "_CON"
    assert safe_segment("a:b*c?") == "a_b_c_"
    assert safe_segment("x. ") == "x"
    long = "я" * 300
    assert len(safe_segment(long)) <= 100
    deep = "https://azbyka.ru/" + "/".join(["очень-длинный-сегмент-пути"] * 20) + "/file.pdf"
    p = url_to_relpath(deep, "application/pdf")
    assert len(p) <= 230 and p.endswith("file.pdf")


def test_kind_by_ext():
    assert kind_by_ext("https://x/a.mp3") == "media"
    assert kind_by_ext("https://x/a.fb2.zip") == "media"
    assert kind_by_ext("https://x/a.PNG") == "asset"
    assert kind_by_ext("https://x/a/") is None


def test_html_links():
    html = """<html><head><title> Тест </title><base href="https://azbyka.ru/base/">
    <link rel="stylesheet" href="/s.css"><link rel="canonical" href="/canon">
    <meta property="og:image" content="/og.jpg"></head><body style="background:url('/bg.png')">
    <a href="page1">1</a><a href="/f.pdf">pdf</a><img src="i.png" srcset="i1.png 1x, i2.png 2x" data-src="/lazy.jpg">
    <audio><source src="/x.mp3"></audio><iframe src="https://www.youtube.com/embed/abc"></iframe>
    <div data-file="/files/book.epub"></div>
    <script class="wp-playlist-script" type="application/json">{"tracks":[{"src":"https:\\/\\/azbyka.ru\\/audio\\/t.mp3"}]}</script>
    <style>.a{background:url(/css-bg.gif)}</style></body></html>"""
    info = html_links(html)
    got = {(lk.url, lk.kind) for lk in info.links}
    assert info.title == "Тест"
    assert info.base == "https://azbyka.ru/base/"
    assert ("page1", "page") in got
    assert ("/f.pdf", "media") in got
    assert ("/s.css", "asset") in got
    assert ("i2.png", "asset") in got
    assert ("/lazy.jpg", "asset") in got
    assert ("/x.mp3", "media") in got
    assert ("https://www.youtube.com/embed/abc", "embed") in got
    assert ("/files/book.epub", "media") in got
    assert ("https://azbyka.ru/audio/t.mp3", "media") in got
    assert ("/css-bg.gif", "asset") in got
    assert ("/bg.png", "asset") in got
    assert ("/og.jpg", "asset") in got


def test_text_links_and_sitemap():
    t = 'var a = "/media/x.m4b"; var b = "https:\\/\\/azbyka.ru\\/y"; var re = "/not-a-file";'
    urls = {lk.url for lk in text_links(t)}
    assert "/media/x.m4b" in urls and "https://azbyka.ru/y" in urls and "/not-a-file" not in urls
    sm = b"<?xml version='1.0'?><urlset><url><loc>https://azbyka.ru/a</loc></url><url><loc><![CDATA[https://azbyka.ru/b?x=1&amp;y=2]]></loc></url></urlset>"
    links, _ = extract_links(sm, "application/xml", "https://azbyka.ru/sitemap.xml")
    assert [lk.url for lk in links] == ["https://azbyka.ru/a", "https://azbyka.ru/b?x=1&y=2"]
    idx = b"<sitemapindex><sitemap><loc>https://azbyka.ru/s1.xml</loc></sitemap></sitemapindex>"
    links, _ = extract_links(idx, "text/xml", "https://azbyka.ru/sitemap.xml")
    assert links[0].kind == "sitemap"
    m3u, _ = extract_links(b"#EXTM3U\n#EXTINF:1,a\nhttps://azbyka.ru/1.mp3\n", "audio/x-mpegurl", "https://azbyka.ru/p.m3u")
    assert m3u[0].url == "https://azbyka.ru/1.mp3" and m3u[0].kind == "media"


def test_robots():
    txt = """
User-agent: Yandex
Disallow: /yandex-only/

User-agent: *
Disallow: /search
Disallow: /*?print=
Disallow: /private/
Allow: /private/ok$
Crawl-delay: 2
Clean-param: sid&ref /
Sitemap: https://azbyka.ru/sitemap.xml
"""
    r = Robots(txt, "azbyka-reserv")
    assert not r.allowed("https://azbyka.ru/search?q=1")
    assert not r.allowed("https://azbyka.ru/a/b?print=1")
    assert not r.allowed("https://azbyka.ru/private/x")
    assert r.allowed("https://azbyka.ru/private/ok")
    assert not r.allowed("https://azbyka.ru/private/ok2")
    assert r.allowed("https://azbyka.ru/yandex-only/")
    assert r.crawl_delay == 2
    assert r.sitemaps == ["https://azbyka.ru/sitemap.xml"]
    assert set(r.params_to_clean("https://azbyka.ru/x")) == {"sid", "ref"}


def test_default_config_rules():
    cfg = load_config()
    assert cfg.excluded("https://azbyka.ru/forum/threads/x.1/")
    assert cfg.excluded("https://azbyka.ru/znakomstva/")
    assert cfg.excluded("https://azbyka.ru/otechnik/login/")
    assert cfg.excluded("https://azbyka.ru/audio/feed/")
    assert cfg.excluded("https://azbyka.ru/sear/?text=1")
    assert not cfg.excluded("https://azbyka.ru/otechnik/Ignatij_Brjanchaninov/otechnik/2")
    assert not cfg.excluded("https://azbyka.ru/biblia/?Mt.1:1&c~r")
    # кириллица в правилах работает и для %-кодированных URL
    assert cfg.excluded("https://azbyka.ru/palomnik/%D0%A3%D1%87%D0%B0%D1%81%D1%82%D0%BD%D0%B8%D0%BA:Ivan")
    assert not cfg.excluded("https://azbyka.ru/palomnik/%D0%A1%D0%BB%D1%83%D0%B6%D0%B5%D0%B1%D0%BD%D0%B0%D1%8F:%D0%92%D1%81%D0%B5_%D1%81%D1%82%D1%80%D0%B0%D0%BD%D0%B8%D1%86%D1%8B")
    assert cfg.date_filtered("https://azbyka.ru/days/1990-01-01")
    assert not cfg.date_filtered("https://azbyka.ru/days/2026-10-04")
    assert not cfg.date_filtered("https://azbyka.ru/otechnik/x/1990-01-01")
    assert cfg.priority_for("https://azbyka.ru/biblia/?Mt.1") == 10
    assert cfg.priority_for("https://azbyka.ru/cerkovnoe-pravo") == 16
    assert cfg.priority_for("https://azbyka.ru/otechnik/x/y.epub") == 8
    assert cfg.query_cap_for("https://azbyka.ru/biblia/?Mt.1") == 400000


def test_peertube():
    from azbyka_reserv import peertube as pt

    assert pt.map_url("https://tube.azbyka.ru/videos/embed/daa1b623-c925-42fa-978e-0a9e5efc60e2") == (
        "https://tube.azbyka.ru/api/v1/videos/daa1b623-c925-42fa-978e-0a9e5efc60e2", "page")
    assert pt.map_url("https://tube.azbyka.ru/w/abcDEF123")[0].endswith("/api/v1/videos/abcDEF123")
    assert pt.map_url("https://tube.azbyka.ru/c/channel/videos") is None
    assert pt.map_url("https://tube.azbyka.ru/download/videos/x-720.mp4") == ("https://tube.azbyka.ru/download/videos/x-720.mp4", "media")
    video = {
        "name": "Фильм",
        "thumbnailPath": "/lazy-static/thumbnails/a.jpg",
        "files": [],
        "streamingPlaylists": [{"files": [
            {"resolution": {"id": 1080}, "fileDownloadUrl": "https://tube.azbyka.ru/download/streaming-playlists/hls/videos/u-1080-fragmented.mp4"},
            {"resolution": {"id": 720}, "fileDownloadUrl": "https://tube.azbyka.ru/download/streaming-playlists/hls/videos/u-720-fragmented.mp4"},
            {"resolution": {"id": 0}, "fileDownloadUrl": "https://tube.azbyka.ru/download/a-0.mp4"},
        ]}],
    }
    import json

    found, title = pt.handle_api("https://tube.azbyka.ru/api/v1/videos/u", json.dumps(video).encode(), 720)
    assert title == "Фильм"
    assert ("https://tube.azbyka.ru/download/streaming-playlists/hls/videos/u-720-fragmented.mp4", "media") in found
    assert not any("1080" in u for u, _ in found)
    lst = {"total": 120, "data": [{"uuid": "a1b2c3d4"}, {"uuid": "e5f6g7h8"}]}
    found, _ = pt.handle_api(pt.list_url("https://tube.azbyka.ru", 0), json.dumps(lst).encode(), 720)
    urls = [u for u, _ in found]
    assert "https://tube.azbyka.ru/api/v1/videos/a1b2c3d4" in urls
    assert any("start=50" in u for u in urls)


def test_rewrite_and_variants_config():
    from azbyka_reserv.config import load_config

    cfg = load_config(None, {
        "rewrite": [{"pattern": r"^(https://azbyka\.ru/biblia/\?[1-4]?[A-Za-z]+\.\d+):[^&]*(&.*)?$", "replace": r"\1&r"}],
        "variants": [{"pattern": r"^https://azbyka\.ru/biblia/\?([1-4]?[A-Za-z]+\.\d+)&r$",
                      "template": "https://azbyka.ru/biblia/?{1}&{x}", "values": ["c", "utfcs"], "priority": 40}],
    })
    assert cfg.rewrite("https://azbyka.ru/biblia/?Lk.3:23&r") == "https://azbyka.ru/biblia/?Lk.3&r"
    assert cfg.rewrite("https://azbyka.ru/biblia/?Hebr.4:14-5:6") == "https://azbyka.ru/biblia/?Hebr.4&r"
    assert cfg.rewrite("https://azbyka.ru/biblia/?Mt.1&r") == "https://azbyka.ru/biblia/?Mt.1&r"
    v = cfg.variants[0]
    m = v.rx.search("https://azbyka.ru/biblia/?Mt.1&r")
    assert v.template.format(m.group(0), *m.groups(), x="c") == "https://azbyka.ru/biblia/?Mt.1&c"
