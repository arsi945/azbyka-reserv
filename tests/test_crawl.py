import hashlib
import os
import sqlite3

from azbyka_reserv.config import load_config
from azbyka_reserv.crawler import Crawler

from .mock_site import EPUB, MP3, Site


def make_cfg(site: Site, **crawl):
    host = f"127.0.0.1"
    over = {
        "site": {
            "scope_hosts": [host],
            "host_aliases": {},
            "https_hosts": [],
            "start_urls": [site.base + "/"],
            "sitemap_urls": [],
        },
        "crawl": {
            "page_workers": 2, "media_workers": 1, "min_delay": 0.0, "timeout": 5, "max_tries": 3,
            "seed_days": False, "min_free_gb": 0, "respect_robots": True,
            "date_filter_patterns": [r"/days/"], "query_variants_cap": 3, "pause_on_network_error": 1,
            **crawl,
        },
        "rules": {"exclude": [r"/forum/"], "include_override": [], "default_priority": 50},
        "priority": [],
        "query_cap": [],
    }
    return load_config(None, over)


def rows(data):
    conn = sqlite3.connect(os.path.join(data, "state.sqlite"))
    conn.row_factory = sqlite3.Row
    return {r["url"]: dict(r) for r in conn.execute("SELECT * FROM urls")}, conn


def test_full_crawl(tmp_path):
    with Site() as site:
        cfg = make_cfg(site)
        data = str(tmp_path / "data")
        c = Crawler(cfg, data)
        c.run(progress_every=0.3, max_seconds=60)
        assert c.stop_reason.startswith("очередь пуста"), c.stop_reason
        r, conn = rows(data)
        b = site.base
        mirror = os.path.join(data, "mirror")

        def st(path):
            return r[b + path]["status"]

        assert st("/") == "done" and r[b + "/"]["title"] == "Главная"
        assert st("/a") == "done" and st("/b/") == "done"
        assert st("/from-sitemap") == "done"  # из robots -> sitemap
        assert b + "/forum/x" not in r  # исключено конфигом
        assert b + "/private/y" not in r  # robots.txt
        assert b + "/days/1990-01-01" not in r  # вне диапазона дат
        assert st("/days/2026-01-02") == "done"
        assert b + "/a?utm_source=zz" not in r and b + "/a?sid=123" not in r  # мусорные параметры
        assert st("/redir") == "redirect" and r[b + "/redir"]["location"] == b + "/a"
        qs = [u for u in r if "/q?" in u]
        assert len(qs) == 3  # лимит вариантов query
        assert st("/flaky") == "done"  # 503 -> повтор
        assert st("/gone") == "notfound"
        assert st("/protected.pdf") == "auth"
        # кириллица: два написания -> один URL
        ru = [u for u in r if "/palomnik/" in u]
        assert len(ru) == 1 and r[ru[0]]["status"] == "done"
        # файл с именем из Content-Disposition
        book = r[b + "/download/book"]
        assert book["path"].endswith("Книга.epub")
        with open(os.path.join(mirror, *book["path"].split("/")), "rb") as f:
            assert f.read() == EPUB
        # mp3 из JSON плейлиста WordPress и из m3u
        for p in ("/files/t1.mp3", "/files/t2.mp3"):
            row = r[b + p]
            assert row["status"] == "done" and row["kind"] == "media"
            with open(os.path.join(mirror, *row["path"].split("/")), "rb") as f:
                assert hashlib.sha1(f.read()).hexdigest() == hashlib.sha1(MP3).hexdigest()
        # ресурсы страницы, включая картинку из CSS
        assert st("/s.css") == "done" and st("/img/bg.png") == "done" and st("/img/x.png") == "done"
        # встроенное видео записано отдельно, сторонняя страница не обходится
        emb = [e[0] for e in conn.execute("SELECT url FROM embeds")]
        assert "https://www.youtube.com/embed/abc123" in emb
        assert not any("other.example.com" in u for u in r)
        skipped = {x[0] for x in conn.execute("SELECT reason FROM skipped_stats")}
        assert {"exclude", "robots", "date", "query_cap"} <= skipped


def test_resume_partial_download(tmp_path):
    with Site() as site:
        cfg = make_cfg(site)
        data = str(tmp_path / "data")
        c = Crawler(cfg, data)
        # подложить «недокачанный» файл
        part = c._partial_path(site.base + "/files/t1.mp3")
        with open(part, "wb") as f:
            f.write(MP3[:1000])
        c.run(progress_every=0.3, max_seconds=60)
        r, _ = rows(data)
        row = r[site.base + "/files/t1.mp3"]
        assert row["status"] == "done" and row["size"] == len(MP3)
        with open(os.path.join(data, "mirror", *row["path"].split("/")), "rb") as f:
            assert f.read() == MP3
        assert not os.path.exists(part)


def test_restart_continues(tmp_path):
    with Site() as site:
        cfg = make_cfg(site)
        data = str(tmp_path / "data")
        c = Crawler(cfg, data)
        c.run(progress_every=0.3, max_seconds=60)
        c.store.close()
        hits_before = dict(site.hits)
        # повторный запуск: всё уже скачано — новых запросов к страницам почти нет
        c2 = Crawler(make_cfg(site), data)
        c2.run(progress_every=0.3, max_seconds=30)
        assert c2.stop_reason.startswith("очередь пуста")
        assert site.hits.get("/a", 0) == hits_before.get("/a", 0)
