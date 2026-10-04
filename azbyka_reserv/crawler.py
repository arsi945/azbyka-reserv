"""Обходчик: очередь в SQLite, потоки страниц и тяжёлых файлов, вежливый темп."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

from . import extract, peertube
from .config import Config
from .fetcher import Bandwidth, FetchError, Fetcher, RateLimiter, maybe_gunzip
from .fsutil import fs_path, human_bytes, mirror_file, write_atomic
from .robots import Robots
from .store import Store, Task
from .urls import UrlRules, kind_by_ext, path_prefix, url_to_relpath

log = logging.getLogger("azbyka_reserv")

PAGE_KINDS = ("sitemap", "page", "asset")
MEDIA_KINDS = ("media",)
PARSE_CTYPES = (
    "text/html", "application/xhtml+xml", "text/css", "application/xml", "text/xml",
    "application/json", "application/ld+json", "application/javascript", "text/javascript",
    "application/x-javascript", "audio/x-mpegurl", "audio/mpegurl", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "text/plain", "application/x-gzip", "application/gzip",
)
_LOGIN_MARKERS = (b'type="password"', b"type='password'", b"name=\"password\"", b"name=\"pwd\"")


def base_ctype(ctype: str | None) -> str:
    return (ctype or "").split(";", 1)[0].strip().lower()


def strip_params(url: str, params: list[str]) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    drop = {p.lower() for p in params}
    tokens = [t for t in parts.query.split("&") if t.split("=", 1)[0].lower() not in drop]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(tokens), ""))


def host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower()
    for p in patterns:
        p = p.lower().split("/", 1)[0]
        if host == p or host.endswith("." + p):
            return True
    return False


class Crawler:
    def __init__(self, cfg: Config, data_dir: str) -> None:
        self.cfg = cfg
        self.data_dir = os.path.abspath(data_dir)
        self.mirror = os.path.join(self.data_dir, "mirror")
        self.partial_dir = os.path.join(self.data_dir, "partial")
        os.makedirs(fs_path(self.mirror), exist_ok=True)
        os.makedirs(fs_path(self.partial_dir), exist_ok=True)
        self.store = Store(os.path.join(self.data_dir, "state.sqlite"))
        self.rules = UrlRules(
            scope_hosts=cfg.scope_hosts,
            host_aliases=cfg.host_aliases,
            drop_params=cfg.drop_params,
            https_hosts=cfg.https_hosts,
        )
        self.limiter = RateLimiter(cfg.min_delay)
        self.fetcher = Fetcher(
            cfg.user_agent, cfg.timeout, self.limiter, Bandwidth(cfg.bandwidth_limit), cfg.cookies_file
        )
        self.robots: dict[str, Robots] = {}
        self._robots_failed: dict[str, float] = {}
        self._robots_lock = threading.Lock()
        self.stop = threading.Event()
        self._active = 0
        self._active_lock = threading.Lock()
        self._net_errors = 0
        self._forbidden = 0
        self._err_lock = threading.Lock()
        self.stats = {"fetched": 0, "bytes": 0, "errors": 0, "new_urls": 0}
        self._stats_lock = threading.Lock()
        self.stop_reason = ""
        self._skips: dict[tuple[str, str], list] = {}
        self._skips_lock = threading.Lock()

    # ------------------------------------------------------------------ robots
    def robots_for(self, any_url: str) -> Robots:
        """robots.txt для «источника» (схема + хост + порт) данного URL."""
        parts = urlsplit(any_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            r = self.robots.get(origin)
            failed_at = self._robots_failed.get(origin)
            if r is not None and (failed_at is None or time.monotonic() - failed_at < 600):
                return r
        text = ""
        url = f"{origin}/robots.txt"
        failed = False
        try:
            resp = self.fetcher.get(url, stop=self.stop)
            if resp.status == 200 and resp.body is not None:
                text = extract.decode_body(resp.body, resp.ctype)
                self._save_direct(url, resp, "sitemap")
            elif resp.status >= 500 or resp.status == 429:
                failed = True
        except FetchError as e:
            failed = True
            log.warning("robots.txt %s недоступен: %s (повторим позже)", origin, e)
        r = Robots(text, agent=self.cfg.user_agent)
        with self._robots_lock:
            self.robots[origin] = r
            if failed:
                self._robots_failed[origin] = time.monotonic()
            else:
                self._robots_failed.pop(origin, None)
        if r.crawl_delay and r.crawl_delay > self.limiter.min_delay:
            log.info("robots.txt %s: Crawl-delay %.1f с — замедляемся", origin, r.crawl_delay)
            self.limiter.min_delay = min(r.crawl_delay, 10.0)
        return r

    def _save_direct(self, url: str, resp, kind: str) -> None:
        """Сохранить ответ, полученный вне очереди (robots.txt)."""
        self.store.add_urls([(url, None, kind, 1, 0, None)])
        row = self.store.get_by_url(url)
        if row is None or resp.body is None:
            return
        rel = self.store.reserve_path(row["id"], url_to_relpath(url, resp.ctype), url)
        write_atomic(mirror_file(self.mirror, rel), resp.body)
        self.store.finish(
            row["id"], status="done", http_status=resp.status, content_type=resp.ctype,
            size=len(resp.body), path=rel, sha1=resp.sha1,
        )

    # ------------------------------------------------------------------- seeds
    def seed(self) -> int:
        rows = []
        for u in self.cfg.start_urls:
            self._admit_into(rows, u, None, "page", depth=0, parent=None, parent_priority=None)
        for u in self.cfg.sitemap_urls:
            self._admit_into(rows, u, None, "sitemap", depth=0, parent=None, parent_priority=None)
        origins = set()
        for u in self.cfg.start_urls:
            norm = self.rules.normalize(u)
            if norm and self.rules.in_scope(norm[0]):
                p = urlsplit(norm[0])
                origins.add(f"{p.scheme}://{p.netloc}/")
        for origin in sorted(origins):
            for sm in self.robots_for(origin).sitemaps:
                self._admit_into(rows, sm, None, "sitemap", depth=0, parent=None, parent_priority=None)
        if self.cfg.peertube_seed_listing:
            for h in self.cfg.peertube_hosts:
                self._admit_into(rows, peertube.list_url(f"https://{h}"), None, "page", depth=0, parent=None, parent_priority=None)
        templates = list(self.cfg.seed_date_templates)
        if self.cfg.seed_days:
            templates.insert(0, self.cfg.seed_days_template)
        if templates:
            d = self.cfg.date_from
            one = timedelta(days=1)
            while d <= self.cfg.date_to:
                for t in templates:
                    self._admit_into(rows, t.format(date=d.isoformat()), None, "page", depth=1, parent=None, parent_priority=None)
                d += one
        n = self.store.add_urls(rows)
        log.info("стартовые адреса: %d (новых %d)", len(rows), n)
        return n

    # --------------------------------------------------------------- admission
    def _skip(self, reason: str, url: str) -> None:
        key = (reason, path_prefix(url) or urlsplit(url).hostname or "")
        with self._skips_lock:
            cur = self._skips.get(key)
            if cur is None:
                self._skips[key] = [1, url]
            else:
                cur[0] += 1

    def flush_skips(self) -> None:
        with self._skips_lock:
            rows = [(r, p, v[0], v[1]) for (r, p), v in self._skips.items()]
            self._skips.clear()
        self.store.add_skips(rows)

    def _admit_into(self, rows: list, raw_url: str, base: str | None, kind: str, depth: int,
                    parent: int | None, parent_priority: int | None, embeds: list | None = None,
                    external_parent: bool = False) -> None:
        norm = self.rules.normalize(raw_url, base)
        if norm is None:
            return
        url, alt = norm
        host = urlsplit(url).hostname or ""
        if host_matches(host, self.cfg.peertube_hosts):
            mapped = peertube.map_url(url)
            if mapped is None:
                self._skip("peertube", url)
                return
            url, kind = mapped
            alt = None
        in_scope = self.rules.host_in_scope(host)
        if in_scope and self.cfg.respect_robots:
            url = strip_params(url, self.robots_for(url).params_to_clean(url))
        if not in_scope:
            if kind == "embed" or host_matches(host, self.cfg.embed_hosts):
                if embeds is not None:
                    embeds.append(url)
                return
            if host_matches(host, self.cfg.extra_media_hosts):
                kind = kind_by_ext(url) or "media"
            elif kind == "asset" and self.cfg.external_assets and not host_matches(host, self.cfg.asset_host_blocklist):
                pass
            elif kind == "media" and self.cfg.external_assets and not host_matches(host, self.cfg.asset_host_blocklist) and not external_parent:
                # прямые ссылки на файлы на сторонних хостах (например, PDF) — качаем, но не обходим дальше
                pass
            else:
                return
        else:
            if external_parent and kind == "page":
                return  # CSS со стороннего CDN не должен расширять обход
            if kind == "embed":
                kind = "page"
        if self.cfg.excluded(url) or (alt and self.cfg.excluded(alt)):
            self._skip("exclude", url)
            return
        if self.cfg.date_filtered(url):
            self._skip("date", url)
            return
        if (in_scope and self.cfg.respect_robots
                and (self.cfg.robots_scope == "all" or kind in ("page", "sitemap"))
                and not self.robots_for(url).allowed(url)):
            self._skip("robots", url)
            return
        if self.cfg.max_depth and depth > self.cfg.max_depth:
            self._skip("depth", url)
            return
        prio = self.cfg.priority_for(url)
        if prio is None:
            prio = parent_priority if (parent_priority is not None and kind == "asset") else self.cfg.default_priority
        if not in_scope and parent_priority is not None:
            prio = max(prio, parent_priority)
        rows.append((url, alt, kind, prio, depth, parent))

    def _apply_query_caps(self, rows: list) -> list:
        with_q = [r for r in rows if urlsplit(r[0]).query]
        if not with_q:
            return rows
        known = self.store.known([r[0] for r in with_q])
        out = []
        seen: set[str] = set()
        for r in rows:
            url = r[0]
            parts = urlsplit(url)
            if parts.query and url not in known and url not in seen:
                key = f"{parts.netloc}{parts.path}"
                if not self.store.bump_query_count(key, self.cfg.query_cap_for(url)):
                    self._skip("query_cap", url)
                    continue
            seen.add(url)
            out.append(r)
        return out

    def enqueue_links(self, task: Task, links: list[extract.Link], base: str, external_parent: bool) -> int:
        rows: list = []
        embeds: list[str] = []
        for link in links:
            child_depth = task.depth + 1 if link.kind in ("page", "embed") else task.depth
            self._admit_into(rows, link.url, base, link.kind, child_depth, task.id, task.priority,
                             embeds=embeds, external_parent=external_parent)
        rows = self._apply_query_caps(rows)
        if embeds:
            self.store.add_embeds((e, task.url) for e in embeds)
        n = self.store.add_urls(rows)
        with self._stats_lock:
            self.stats["new_urls"] += n
        return n

    # ------------------------------------------------------------------ worker
    def _write_page(self, task: Task, url: str, resp, body: bytes) -> str:
        rel = url_to_relpath(url, resp.ctype, resp.filename)
        rel = self.store.reserve_path(task.id, rel, url)
        dest = mirror_file(self.mirror, rel)
        try:
            write_atomic(dest, body)
        except (NotADirectoryError, FileExistsError, IsADirectoryError, PermissionError):
            rel = self._conflict_path(task, url, rel)
            write_atomic(mirror_file(self.mirror, rel), body)
        return rel

    def _conflict_path(self, task: Task, url: str, rel: str) -> str:
        host, _, rest = rel.partition("/")
        name = rest.rsplit("/", 1)[-1]
        alt = f"{host}/_conflict/{hashlib.sha1(url.encode()).hexdigest()[:12]}/{name}"
        return self.store.reserve_path(task.id, alt, url + "#conflict")

    def _partial_path(self, url: str) -> str:
        return fs_path(os.path.join(self.partial_dir, hashlib.sha1(url.encode()).hexdigest() + ".part"))

    def _note_net_error(self) -> None:
        with self._err_lock:
            self._net_errors += 1
            n = self._net_errors
        if n >= 10 and n % 10 == 0:
            log.warning("Много сетевых ошибок подряд (%d). Сеть недоступна? Пауза %d с.", n, self.cfg.pause_on_network_error)
            self.limiter.pause(self.cfg.pause_on_network_error)

    def _note_ok(self) -> None:
        with self._err_lock:
            self._net_errors = 0
            self._forbidden = 0

    def _retry_or_fail(self, task: Task, error: str, status: int | None = None) -> None:
        tries = task.tries + 1
        if tries >= self.cfg.max_tries:
            self.store.finish(task.id, status="error", error=error, tries=tries, http_status=status)
            with self._stats_lock:
                self.stats["errors"] += 1
            log.info("ошибка (сдались): %s — %s", task.url, error)
        else:
            self.store.requeue(task.id, error, tries)

    def process(self, task: Task) -> None:
        url = task.url
        stream = task.kind == "media"
        tmp = self._partial_path(url) if stream else None
        try:
            resp = self.fetcher.get(
                url,
                to_file=tmp,
                max_memory=self.cfg.max_page_size,
                max_size=self.cfg.max_file_size,
                etag=task.etag if task.path else None,
                last_modified=task.last_modified if task.path else None,
                stop=self.stop,
            )
            if resp.status in (404, 410) and task.alt_url and self.cfg.alias_fallback:
                log.debug("404 по %s, пробуем исходный адрес %s", url, task.alt_url)
                resp = self.fetcher.get(task.alt_url, to_file=tmp, max_memory=self.cfg.max_page_size,
                                        max_size=self.cfg.max_file_size, stop=self.stop)
        except FetchError as e:
            if self.stop.is_set():
                self.store.requeue(task.id, str(e), task.tries)
                return
            if not e.retryable:
                self.store.finish(task.id, status="skipped", error=str(e), http_status=e.status)
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
                return
            if str(e).startswith("network"):
                self._note_net_error()
            self._retry_or_fail(task, str(e))
            return
        self._handle_response(task, resp, tmp)

    def _handle_response(self, task: Task, resp, tmp: str | None) -> None:
        url = task.url
        st = resp.status
        if st in (301, 302, 303, 307, 308):
            self._note_ok()
            target = None
            if resp.location:
                norm = self.rules.normalize(resp.location, url)
                target = norm[0] if norm else None
            self.store.finish(task.id, status="redirect", http_status=st, location=target)
            if target and target != url:
                low = target.lower()
                if task.kind == "media" and any(x in low for x in ("login", "auth", "register", "signin")):
                    self.store.finish(task.id, status="auth", http_status=st, location=target,
                                      error="редирект на вход: нужны куки (cookies_file)")
                    return
                rows: list = []
                self._admit_into(rows, target, None, task.kind if task.kind != "sitemap" else "page",
                                 task.depth, task.id, task.priority)
                self.store.add_urls(self._apply_query_caps(rows))
            return
        if st == 304:
            self._note_ok()
            self.store.finish(task.id, status="done", http_status=304)
            return
        if st in (404, 410):
            self._note_ok()
            self.store.finish(task.id, status="notfound", http_status=st)
            return
        if st == 416 and tmp and os.path.exists(tmp):
            os.remove(tmp)  # частичный файл не совпал — начнём заново
            self.store.requeue(task.id, "416: перезапуск докачки", task.tries + 1)
            return
        if st in (429, 503, 502, 504, 520, 521, 522, 524):
            ra = resp.headers.get("Retry-After") if resp.headers else None
            wait = float(ra) if ra and ra.isdigit() else min(60 * (task.tries + 1), 900)
            log.warning("HTTP %d на %s — пауза %.0f с", st, url, wait)
            self.limiter.pause(wait)
            self._retry_or_fail(task, f"HTTP {st}", st)
            return
        if st in (401, 403):
            with self._err_lock:
                self._forbidden += 1
                nf = self._forbidden
            if nf >= 25 and nf % 25 == 0:
                log.warning("Подряд %d ответов 401/403 — возможна блокировка. Пауза 15 мин.", nf)
                self.limiter.pause(900)
            status = "auth" if task.kind == "media" else "error"
            self.store.finish(task.id, status=status, http_status=st, error=f"HTTP {st}", tries=task.tries + 1)
            return
        if st not in (200, 203, 206):
            self._retry_or_fail(task, f"HTTP {st}", st)
            return
        self._note_ok()
        ct = base_ctype(resp.ctype)

        # Файл (потоковое скачивание)
        if tmp is not None:
            if ct in ("text/html", "application/xhtml+xml"):
                with open(tmp, "rb") as f:
                    head = f.read(min(resp.size or 0, self.cfg.max_page_size) or self.cfg.max_page_size)
                os.remove(tmp)
                if any(m in head for m in _LOGIN_MARKERS):
                    self.store.finish(task.id, status="auth", http_status=st, content_type=resp.ctype,
                                      error="вместо файла страница входа: нужны куки (cookies_file)")
                    return
                # это оказалась страница — обработать как страницу
                self.store.execute("UPDATE urls SET kind='page' WHERE id=?", (task.id,))
                resp.body = head
                self._finish_page(task, resp, head)
                return
            rel = url_to_relpath(url, resp.ctype, resp.filename)
            rel = self.store.reserve_path(task.id, rel, url)
            dest = mirror_file(self.mirror, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(tmp, dest)
            except (NotADirectoryError, FileExistsError, IsADirectoryError, PermissionError):
                rel = self._conflict_path(task, url, rel)
                dest = mirror_file(self.mirror, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(tmp, dest)
            self._done(task, resp, rel)
            return

        # Страница / ресурс в памяти
        self._finish_page(task, resp, resp.body or b"")

    def _finish_page(self, task: Task, resp, body: bytes) -> None:
        url = task.url
        rel = self._write_page(task, url, resp, body)
        title = None
        ct = base_ctype(resp.ctype)
        external = not self.rules.in_scope(url)
        should_parse = (ct in PARSE_CTYPES or task.kind == "sitemap") and not self.cfg.no_follow(url)
        if external and ct != "text/css":
            should_parse = False
        up = urlsplit(url)
        if host_matches(up.hostname or "", self.cfg.peertube_hosts) and up.path.startswith("/api/"):
            found, title = peertube.handle_api(url, body, self.cfg.peertube_max_height)
            rows: list = []
            for u, k in found:
                self._admit_into(rows, u, None, k, task.depth + 1, task.id, task.priority)
            self.store.add_urls(self._apply_query_caps(rows))
            should_parse = False
        if should_parse and len(body) <= self.cfg.max_page_size:
            data = maybe_gunzip(body, url, resp.ctype)
            pctype = resp.ctype
            if data is not body:
                pctype = "application/xml"
            if ct == "text/plain" and not url.lower().endswith("/robots.txt"):
                links, meta = [], {}
            else:
                links, meta = extract.extract_links(data, pctype, url)
            title = meta.get("title") or None
            base = url
            if meta.get("base"):
                norm = self.rules.normalize(meta["base"], url)
                if norm:
                    base = norm[0]
            if links:
                self.enqueue_links(task, links, base, external_parent=external)
        self._done(task, resp, rel, title)

    def _done(self, task: Task, resp, rel: str, title: str | None = None) -> None:
        h = resp.headers
        self.store.finish(
            task.id, status="done", http_status=resp.status, content_type=resp.ctype, size=resp.size,
            path=rel, sha1=resp.sha1, etag=h.get("ETag") if h else None,
            last_modified=h.get("Last-Modified") if h else None, title=title, error=None,
        )
        with self._stats_lock:
            self.stats["fetched"] += 1
            self.stats["bytes"] += resp.size or 0

    # --------------------------------------------------------------------- run
    def _disk_ok(self) -> bool:
        if self.cfg.min_free_bytes <= 0:
            return True
        try:
            free = shutil.disk_usage(self.data_dir).free
        except OSError:
            return True
        if free < self.cfg.min_free_bytes:
            self.stop_reason = f"мало места на диске: свободно {human_bytes(free)}"
            log.error("Остановка: %s", self.stop_reason)
            self.stop.set()
            return False
        return True

    def _worker(self, lanes: list[tuple[str, ...]]) -> None:
        idle_sleep = 1.0
        n = 0
        while not self.stop.is_set():
            task = None
            for kinds in lanes:
                got = self.store.claim(kinds, 1)
                if got:
                    task = got[0]
                    break
            if task is None:
                self.stop.wait(idle_sleep)
                continue
            with self._active_lock:
                self._active += 1
            try:
                n += 1
                if task.kind == "media" or n % 50 == 0:
                    if not self._disk_ok():
                        self.store.requeue(task.id, "", task.tries)
                        break
                self.process(task)
            except Exception as e:  # noqa: BLE001 — один плохой URL не должен ронять поток
                log.exception("сбой при обработке %s", task.url)
                self._retry_or_fail(task, f"internal: {e!r}")
            finally:
                with self._active_lock:
                    self._active -= 1

    def queued_count(self) -> int:
        return self.store.query("SELECT COUNT(*) FROM urls WHERE status IN ('queued','active')")[0][0]

    def run(self, progress_every: float = 15.0, max_seconds: float = 0) -> None:
        reset = self.store.reset_active()
        if reset:
            log.info("возвращено в очередь после прошлого запуска: %d", reset)
        self.seed()
        threads = []
        for i in range(self.cfg.page_workers):
            t = threading.Thread(target=self._worker, args=([PAGE_KINDS],), name=f"page-{i}", daemon=True)
            threads.append(t)
        for i in range(self.cfg.media_workers):
            t = threading.Thread(target=self._worker, args=([MEDIA_KINDS, PAGE_KINDS],), name=f"media-{i}", daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        started = time.monotonic()
        last_bytes = 0
        last_fetched = 0
        last_t = started
        idle_checks = 0
        try:
            while not self.stop.is_set():
                self.stop.wait(progress_every)
                now = time.monotonic()
                with self._stats_lock:
                    fetched, nbytes = self.stats["fetched"], self.stats["bytes"]
                dt = max(now - last_t, 1e-6)
                counts = self.store.counts()
                queued = counts.get("queued", 0)
                with self._active_lock:
                    active = self._active
                pause = self.limiter.paused_for()
                log.info(
                    "готово %d | в очереди %d | ошибок %d | нужен вход %d | сохранено за сеанс %s | %.1f стр/с | %s/с%s",
                    counts.get("done", 0), queued, counts.get("error", 0), counts.get("auth", 0),
                    human_bytes(nbytes), (fetched - last_fetched) / dt, human_bytes((nbytes - last_bytes) / dt),
                    f" | пауза {pause:.0f} с" if pause > 0 else "",
                )
                last_bytes, last_fetched, last_t = nbytes, fetched, now
                self.flush_skips()
                if queued == 0 and active == 0:
                    idle_checks += 1
                    if idle_checks >= 2:
                        self.stop_reason = "очередь пуста — обход завершён"
                        log.info(self.stop_reason)
                        break
                else:
                    idle_checks = 0
                if max_seconds and now - started > max_seconds:
                    self.stop_reason = "достигнут лимит времени сеанса"
                    break
        except KeyboardInterrupt:
            self.stop_reason = "прервано пользователем (Ctrl+C)"
            log.info("Остановка по Ctrl+C: дожидаемся текущих запросов…")
        finally:
            self.stop.set()
            for t in threads:
                t.join(timeout=30)
            self.store.reset_active()
            self.flush_skips()
            self.store.set_meta("last_run_end", str(time.time()))


def days_range(d1: date, d2: date):
    d = d1
    while d <= d2:
        yield d
        d += timedelta(days=1)
