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


def test_variants_and_rewrite_in_crawl(tmp_path):
    with Site() as site:
        b = site.base
        esc = b.replace(".", r"\.")
        cfg = make_cfg(site)
        cfg.raw["rewrite"] = [{"pattern": "^(" + esc + r"/q)\?x=\d+$", "replace": r"\1?x=1"}]
        cfg.raw["variants"] = [{"pattern": "^" + esc + r"/a$", "template": b + "/a-{x}", "values": ["one", "two"], "priority": 5}]
        from azbyka_reserv.config import Config

        cfg = Config(cfg.raw)
        data = str(tmp_path / "data")
        c = Crawler(cfg, data)
        c.run(progress_every=0.3, max_seconds=60)
        r, _ = rows(data)
        assert [u for u in r if "/q?" in u] == [b + "/q?x=1"]  # все ?x=N свернулись в один
        assert r[b + "/a-one"]["priority"] == 5 and r[b + "/a-two"]["status"] == "notfound"


def test_relink_picks_up_new_rules(tmp_path):
    with Site() as site:
        data = str(tmp_path / "data")
        c = Crawler(make_cfg(site), data)
        c.run(progress_every=0.3, max_seconds=60)
        c.store.close()
        r, _ = rows(data)
        assert site.base + "/forum/x" not in r
        # новые правила: форум больше не исключён -> relink находит ссылку без сети
        cfg2 = make_cfg(site)
        cfg2.raw["rules"]["exclude"] = []
        from azbyka_reserv.config import Config

        c2 = Crawler(Config(cfg2.raw), data)
        n = c2.relink()
        assert n >= 1
        r, _ = rows(data)
        assert r[site.base + "/forum/x"]["status"] == "queued"


def test_network_outage_does_not_burn_tries(tmp_path):
    import threading
    import time as _t

    with Site() as site:
        cfg = make_cfg(site, pause_on_network_error=1, max_tries=2)
        data = str(tmp_path / "data")
        c = Crawler(cfg, data)
        # «сеть пропала» сразу после старта: сервер выключается
        def kill():
            _t.sleep(0.3)
            site.server.shutdown()
            site.server.server_close()
        threading.Thread(target=kill, daemon=True).start()
        c.run(progress_every=0.3, max_seconds=6)
        c.store.close()
        _, conn = rows(data)
        errs = conn.execute("SELECT COUNT(*) FROM urls WHERE status='error'").fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM urls WHERE status='queued'").fetchone()[0]
        assert queued > 0
        assert errs <= 2  # первые пара ошибок могли засчитаться до определения «нет сети»
