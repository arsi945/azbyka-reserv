"""Обходчик: очередь в SQLite, потоки страниц и тяжёлых файлов, вежливый темп."""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

from . import extract, peertube
from .config import Config
from .fetcher import Bandwidth, FetchError, Fetcher, RateLimiter, file_sha1, maybe_gunzip, parse_retry_after, remove_partial
from .fsutil import fs_path, human_bytes, mirror_file, write_atomic
from .robots import Robots
from .store import Store, Task
from .urls import UrlRules, kind_by_ext, path_prefix, url_ext, url_to_relpath

log = logging.getLogger("azbyka_reserv")

# Версия логики извлечения/допуска ссылок. Меняется, когда новая версия
# программы находит ссылки иначе, — тогда скачанное разбирается заново (relink).
EXTRACT_VERSION = "4"

PAGE_KINDS = ("sitemap", "page", "asset")
MEDIA_KINDS = ("media",)
PARSE_CTYPES = (
    "text/html", "application/xhtml+xml", "text/css", "application/xml", "text/xml",
    "application/json", "application/ld+json", "application/javascript", "text/javascript",
    "application/x-javascript", "audio/x-mpegurl", "audio/mpegurl", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "text/plain", "application/x-gzip", "application/gzip",
)
_LOGIN_MARKERS = (b'type="password"', b"type='password'", b"name=\"password\"", b"name=\"pwd\"")
_AUTH_PATH = re.compile(r"/(auth|login|signin|sign-in|register|registration)(/|\?|$)")
# сигнатуры двоичных файлов: сервер иногда шлёт их с Content-Type: text/html
_BINARY_MAGIC = (b"%PDF", b"PK\x03\x04", b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"AT&TFORM",
                 b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"OggS", b"fLaC", b"\xd0\xcf\x11\xe0",
                 b"Rar!", b"7z\xbc\xaf")

_CHALLENGE_MARKERS = (
    b"ddos-guard.net/", b"check.ddos-guard", b"DDoS-Guard</title>", b"__ddg_challenge",
    b"<title>Just a moment...</title>", b"cf-challenge", b"challenge-platform", b"cf_chl_opt",
)


def is_challenge(body: bytes) -> bool:
    """Страница-заглушка DDoS-Guard/Cloudflare (маленькая, с характерными метками)."""
    if len(body) > 60_000:
        return False
    return any(m in body for m in _CHALLENGE_MARKERS)


def looks_binary(head: bytes) -> bool:
    return head[:12].lstrip().startswith(_BINARY_MAGIC) or head[4:8] == b"ftyp"


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


_EMBED_PATH = re.compile(r"(?i)/(embed|video_ext\.php|play/embed|videoembed|player|watch|shorts|w|v|video)(/|$|\?)")
_EMPTY_EMBED = re.compile(r"(?i)/embed/?(\?|$)")


def url_blocked(url: str, patterns: list[str]) -> bool:
    """Запись вида 'host' блокирует хост (и поддомены), 'host/путь' — только этот префикс."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for p in patterns:
        p = p.lower()
        if "/" in p:
            ph, _, pp = p.partition("/")
            if (host == ph or host.endswith("." + ph)) and parts.path.lower().startswith("/" + pp):
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


def host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower()
    for p in patterns:
        p = p.lower().split("/", 1)[0]
        if host == p or host.endswith("." + p):
            return True
    return False


def _is_path_conflict(dest: str) -> bool:
    """Путь занят «не тем»: dest — папка, или одна из родительских папок — файл."""
    if os.path.isdir(dest):
        return True
    d = os.path.dirname(dest)
    while d and not os.path.exists(d):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return bool(d) and os.path.isfile(d)


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
        self._err_lock = threading.Lock()
        self._forbidden_times: collections.deque = collections.deque(maxlen=50)
        self._server_err: collections.deque = collections.deque(maxlen=50)
        self._tls_errors = 0
        self._probe_lock = threading.Lock()
        self._probe_at = 0.0
        self._probe_ok = True
        self.stats = {"fetched": 0, "bytes": 0, "errors": 0, "new_urls": 0}
        self._stats_lock = threading.Lock()
        self.stop_reason = ""
        self._skips: dict[tuple[str, str], list] = {}
        self._skips_lock = threading.Lock()
        main = self.rules.normalize(cfg.start_urls[0]) if cfg.start_urls else None
        p = urlsplit(main[0]) if main else None
        self.main_origin = f"{p.scheme}://{p.netloc}" if p else "https://azbyka.ru"

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
        text, failed = self._fetch_robots(origin)
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

    def _fetch_robots(self, origin: str) -> tuple[str, bool]:
        """(текст, сбой?) — с переходами по редиректам и повторами."""
        url = f"{origin}/robots.txt"
        for attempt in range(3):
            if self.stop.is_set():
                break
            try:
                target = url
                resp = None
                for _ in range(4):  # следуем за редиректами
                    resp = self.fetcher.get(target, stop=self.stop)
                    if resp.status in (301, 302, 303, 307, 308) and resp.location:
                        norm = self.rules.normalize(resp.location, target)
                        if not norm:
                            break
                        target = norm[0]
                        continue
                    break
                if resp is None:
                    break
                if resp.status == 200 and resp.body is not None:
                    if base_ctype(resp.ctype) in ("text/html", "application/xhtml+xml") or is_challenge(resp.body):
                        log.warning("robots.txt %s: вместо файла пришла HTML-страница", origin)
                    else:
                        self._save_direct(url, resp, "sitemap")
                        return extract.decode_body(resp.body, resp.ctype), False
                elif resp.status in (404, 410):
                    return "", False  # robots.txt нет — ограничений нет
                else:
                    log.warning("robots.txt %s: HTTP %d", origin, resp.status)
            except FetchError as e:
                log.warning("robots.txt %s недоступен: %s", origin, e)
            if attempt < 2:
                self.stop.wait(5 * (attempt + 1))
        log.warning("robots.txt %s получить не удалось — повторим через 10 минут", origin)
        return "", True

    def _save_direct(self, url: str, resp, kind: str) -> None:
        """Сохранить ответ, полученный вне очереди (robots.txt)."""
        self.store.add_urls([(url, None, kind, 1, 0, None)])
        row = self.store.get_by_url(url)
        if row is None or resp.body is None:
            return
        rel = self.store.reserve_path(row["id"], url_to_relpath(url, resp.ctype), url)
        try:
            write_atomic(mirror_file(self.mirror, rel), resp.body)
        except OSError as e:
            log.warning("не удалось сохранить %s: %s", url, e)
            return
        self.store.finish(
            row["id"], status="done", http_status=resp.status, content_type=resp.ctype,
            size=len(resp.body), path=rel, sha1=resp.sha1,
        )

    # ------------------------------------------------------------------- seeds
    def seed(self) -> int:
        rows: list = []
        for u in self.cfg.start_urls:
            self._admit_into(rows, u, None, kind_by_ext(u) or "page", depth=0, parent=None, parent_priority=None)
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
                self._admit_into(rows, peertube.list_url(f"https://{h}"), None, "page", depth=0,
                                 parent=None, parent_priority=None)
        specs = self._date_specs()
        sig = hashlib.sha1(repr([(s["template"], str(s["from"]), str(s["to"]), s.get("priority")) for s in specs])
                           .encode()).hexdigest()
        if self.store.get_meta("date_seed_sig") != sig:
            for spec in specs:
                for d in dates_nearest_first(spec["from"], spec["to"], date.today()):
                    self._admit_into(rows, spec["template"].format(date=d.isoformat()), None, "page", depth=1,
                                     parent=None, parent_priority=None, priority_override=spec.get("priority"))
        rows = self._expand_variants(rows)
        n = self.store.add_urls(rows)
        self.store.set_meta("date_seed_sig", sig)
        log.info("стартовые адреса: %d (новых %d)", len(rows), n)
        self._expand_known_variants()
        self.flush_skips()
        return n

    def _date_specs(self) -> list[dict]:
        specs: list[dict] = []
        raw = list(self.cfg.seed_date_templates)
        if self.cfg.seed_days:
            raw.insert(0, self.cfg.seed_days_template)
        for t in raw:
            spec = dict(t) if isinstance(t, dict) else {"template": t}
            d1 = max(self.cfg.date_from, date.fromisoformat(str(spec["from"]))) if "from" in spec else self.cfg.date_from
            d2 = min(self.cfg.date_to, date.fromisoformat(str(spec["to"]))) if "to" in spec else self.cfg.date_to
            spec["from"], spec["to"] = d1, d2
            if "priority" in spec:
                spec["priority"] = int(spec["priority"])
            specs.append(spec)
        return specs

    def _expand_known_variants(self) -> None:
        """Если правила [[variants]] изменились — применить их и к уже известным URL."""
        sig = hashlib.sha1(repr([(v.rx.pattern, v.template, v.values, v.priority) for v in self.cfg.variants])
                           .encode()).hexdigest()
        if not self.cfg.variants or self.store.get_meta("variants_sig") == sig:
            return
        log.info("правила вариантов изменились — дополняем очередь для уже известных адресов…")
        total = 0
        batch: list = []
        for r in self.store.iter_chunks("id, url, kind, priority, depth"):
            if any(v.rx.search(r[1]) for v in self.cfg.variants):
                batch.append((r[1], None, r[2], r[3], r[4], r[0]))
            if len(batch) >= 2000:
                total += self.store.add_urls(self._expand_variants(batch, force=True))
                batch = []
        if batch:
            total += self.store.add_urls(self._expand_variants(batch, force=True))
        self.store.set_meta("variants_sig", sig)
        log.info("добавлено вариантов: %d", total)

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
                    external_parent: bool = False, priority_override: int | None = None) -> None:
        norm = self.rules.normalize(raw_url, base)
        if norm is None:
            return
        url, alt = norm
        if self.cfg.rewrites:
            new = self.cfg.rewrite(url)
            if new != url:
                norm = self.rules.normalize(new)
                if norm is None:
                    return
                url, alt = norm[0], None
        host = urlsplit(url).hostname or ""
        if host_matches(host, self.cfg.peertube_hosts):
            mapped = peertube.map_url(url)
            if mapped is None:
                self._skip("peertube", url)
                return
            url, kind = mapped
            alt = None
        in_scope = self.rules.host_in_scope(host)
        if in_scope and self.cfg.respect_robots and self.cfg.apply_clean_param:
            url = strip_params(url, self.robots_for(url).params_to_clean(url))
        if not in_scope:
            if host_matches(host, self.cfg.embed_hosts):
                # только настоящие плееры, а не кнопки «поделиться» (vk.com/share.php, ok.ru/offer…)
                if embeds is not None and (kind == "embed" or _EMBED_PATH.search(urlsplit(url).path)) \
                        and not _EMPTY_EMBED.search(url):
                    embeds.append(url)
                return
            if kind == "embed":
                return  # прочие сторонние iframe (карты, виджеты) не нужны
            blocked = url_blocked(url, self.cfg.asset_host_blocklist)
            if host_matches(host, self.cfg.extra_media_hosts):
                kind = kind_by_ext(url) or "media"
            elif kind == "asset" and self.cfg.external_assets and not blocked:
                pass
            elif kind == "media" and self.cfg.external_assets and not blocked and not external_parent:
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
        if kind in ("page", "sitemap") and self.cfg.date_filtered(url):
            self._skip("date", url)
            return
        if (in_scope and self.cfg.respect_robots
                and (self.cfg.robots_scope == "all" or kind in ("page", "sitemap"))
                and not self.cfg.robots_overridden(url)
                and not self.robots_for(url).allowed(url)):
            self._skip("robots", url)
            return
        if not self.cfg.cookies_file and self.cfg.needs_login(url):
            # без входа сайт всё равно ответит 302 -> /auth; не тратим запросы.
            # Появится cookies_file — авто-relink поставит их в очередь.
            self._skip("login", url)
            return
        if self.cfg.max_depth and depth > self.cfg.max_depth:
            self._skip("depth", url)
            return
        if priority_override is not None:
            prio = priority_override
        elif kind == "asset":
            # картинки/стили/шрифты идут вместе со своей страницей, а не по правилам раздела;
            # найденные только в картах сайта (<image:loc>) — в конце очереди
            prio = parent_priority if parent_priority is not None else self.cfg.asset_default_priority
        else:
            prio = self.cfg.priority_for(url)
            if prio is None:
                prio = self.cfg.default_priority
            if not in_scope and parent_priority is not None:
                prio = max(prio, parent_priority)
        rows.append((url, alt, kind, prio, depth, parent))

    def _expand_variants(self, rows: list, force: bool = False) -> list:
        """Для новых URL, подходящих под [[variants]], добавить их варианты
        (например, ту же главу Библии во всех переводах)."""
        if not self.cfg.variants:
            return rows
        cands = [r for r in rows if any(v.rx.search(r[0]) for v in self.cfg.variants)]
        if not cands:
            return rows
        known = set() if force else self.store.known(list({r[0] for r in cands}))
        extra: list = []
        seen: set[str] = set()
        for r in cands:
            url = r[0]
            if url in known or url in seen:
                continue
            seen.add(url)
            for v in self.cfg.variants:
                m = v.rx.search(url)
                if not m:
                    continue
                args = [m.group(0), *[g or "" for g in m.groups()]]
                for val in v.values:
                    try:
                        vu = v.template.format(*args, x=val)
                    except (IndexError, KeyError):
                        continue
                    if vu != url:
                        self._admit_into(extra, vu, None, r[2], r[4], r[5], None, priority_override=v.priority)
        return rows + extra

    def _apply_query_caps(self, rows: list) -> list:
        rows = self._expand_variants(rows)
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
        # Ссылки из карт сайта и robots.txt НЕ наследуют их высший приоритет:
        # иначе тысячи картинок из <image:loc> встали бы в очередь раньше страниц.
        parent_prio = None if task.kind == "sitemap" else task.priority
        for link in links:
            child_depth = task.depth + 1 if link.kind in ("page", "embed") else task.depth
            self._admit_into(rows, link.url, base, link.kind, child_depth, task.id, parent_prio,
                             embeds=embeds, external_parent=external_parent)
        rows = self._apply_query_caps(rows)
        if embeds:
            self.store.add_embeds((e, task.url) for e in embeds)
        n = self.store.add_urls(rows)
        with self._stats_lock:
            self.stats["new_urls"] += n
        return n

    # ------------------------------------------------------------ файлы и пути
    def _conflict_path(self, task: Task, url: str, rel: str) -> str:
        host, _, rest = rel.partition("/")
        name = rest.rsplit("/", 1)[-1]
        alt = f"{host}/_conflict/{hashlib.sha1(url.encode()).hexdigest()[:12]}/{name}"
        return self.store.reserve_path(task.id, alt, url + "#conflict")

    def _store_file(self, task: Task, url: str, rel: str, write) -> str:
        """Записать файл в зеркало: ``write(dest)`` выполняет запись/перенос.

        PermissionError на Windows обычно временный (антивирус, индексатор,
        открытый просмотрщиком файл) — несколько повторов. Перенос в
        _conflict/ — только при настоящем конфликте «файл против папки».
        """
        dest = mirror_file(self.mirror, rel)
        for attempt in range(6):
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                write(dest)
                return rel
            except PermissionError:
                if _is_path_conflict(dest):
                    break
                if attempt == 5:
                    raise
                time.sleep(0.5 * (attempt + 1))
            except (NotADirectoryError, FileExistsError, IsADirectoryError, FileNotFoundError):
                if not _is_path_conflict(dest):
                    raise
                break
        rel = self._conflict_path(task, url, rel)
        dest = mirror_file(self.mirror, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        write(dest)
        return rel

    def _write_page(self, task: Task, url: str, resp, body: bytes) -> str:
        rel = self.store.reserve_path(task.id, url_to_relpath(url, resp.ctype, resp.filename), url)

        def write(dest: str) -> None:
            try:
                write_atomic(dest, body)
            except BaseException:
                try:
                    os.remove(dest + ".tmp")
                except OSError:
                    pass
                raise

        return self._store_file(task, url, rel, write)

    def _move_file(self, task: Task, url: str, resp, src: str) -> str:
        rel = self.store.reserve_path(task.id, url_to_relpath(url, resp.ctype, resp.filename), url)
        rel = self._store_file(task, url, rel, lambda dest: os.replace(src, dest))
        remove_partial(src)  # убрать файл-спутник с метаданными докачки
        return rel

    def _partial_path(self, url: str, suffix: str = ".part") -> str:
        return fs_path(os.path.join(self.partial_dir, hashlib.sha1(url.encode()).hexdigest() + suffix))

    def _sweep_partials(self) -> None:
        """Удалить недокачанные файлы, которые больше никому не нужны."""
        try:
            names = os.listdir(fs_path(self.partial_dir))
        except OSError:
            return
        if not names:
            return
        wanted: set[str] = set()
        for r in self.store.iter_chunks("id, url", "kind='media' AND status IN ('queued','active','error')"):
            wanted.add(hashlib.sha1(r[1].encode()).hexdigest())
        removed = 0
        for n in names:
            stem = n.split(".", 1)[0]
            if stem not in wanted or n.endswith(".spill"):
                try:
                    os.remove(os.path.join(fs_path(self.partial_dir), n))
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("удалено ненужных недокачанных файлов: %d", removed)

    # ---------------------------------------------------------------- ошибки
    def _backoff(self, tries: int) -> float:
        return float(min(self.cfg.retry_backoff * 2 ** max(tries - 1, 0), 3600))

    def _retry_or_fail(self, task: Task, error: str, status: int | None = None, spend: bool = True,
                       delay: float | None = None) -> None:
        tries = task.tries + (1 if spend else 0)
        if tries >= self.cfg.max_tries:
            self.store.finish(task.id, status="error", error=error, tries=tries, http_status=status)
            with self._stats_lock:
                self.stats["errors"] += 1
            log.info("ошибка (сдались): %s — %s", task.url, error)
        else:
            self.store.requeue(task.id, error, tries, delay=self._backoff(tries) if delay is None else delay)

    def _site_reachable(self) -> bool:
        """Отвечает ли главный сайт (проверка не чаще раза в минуту)."""
        with self._probe_lock:
            if time.monotonic() - self._probe_at < 60:
                return self._probe_ok
            self._probe_at = time.monotonic()
            try:
                resp = self.fetcher.get(self.main_origin + "/robots.txt", stop=self.stop)
                self._probe_ok = resp.status < 500
            except FetchError as e:
                self._probe_ok = e.kind not in ("network", "tls")
            if not self._probe_ok:
                log.warning("Сайт %s не отвечает — похоже, пропал интернет. Пауза %d с, затем продолжим.",
                            self.main_origin, self.cfg.pause_on_network_error)
                self.limiter.pause(self.cfg.pause_on_network_error)
            return self._probe_ok

    def _handle_fetch_error(self, task: Task, e: FetchError, tmp: str | None) -> None:
        if self.stop.is_set() or e.kind == "stopped":
            self.store.requeue(task.id, str(e), task.tries)
            return
        if not e.retryable:
            self.store.finish(task.id, status="skipped", error=str(e), http_status=e.status)
            if tmp:
                remove_partial(tmp)
            return
        in_scope = self.rules.in_scope(task.url)
        if e.kind == "tls" and in_scope:
            with self._err_lock:
                self._tls_errors += 1
                n = self._tls_errors
            if n >= 3:
                self.stop_reason = ("tls: не удаётся проверить сертификат сайта. Установите пакеты: "
                                    "py -3 -m pip install truststore certifi — и запустите снова")
                log.error("Остановка: %s", self.stop_reason)
                self.stop.set()
            self.store.requeue(task.id, str(e), task.tries)
            return
        if e.kind == "network" and in_scope and not self._site_reachable():
            # пропала сеть целиком: не тратим попытки, вернёмся после паузы
            self.store.requeue(task.id, str(e), task.tries, delay=60)
            return
        if e.kind == "truncated" and tmp and "не тот кусок" in str(e):
            remove_partial(tmp)
        self._retry_or_fail(task, str(e))

    def _note_forbidden(self) -> None:
        now = time.monotonic()
        with self._err_lock:
            self._forbidden_times.append(now)
            recent = [t for t in self._forbidden_times if now - t < 120]
        if len(recent) >= 10:
            log.warning("Много ответов 401/403 за 2 минуты — возможна временная блокировка. Пауза 10 мин.")
            self.limiter.pause(600)
            with self._err_lock:
                self._forbidden_times.clear()

    def _note_server_error(self, url: str) -> None:
        now = time.monotonic()
        with self._err_lock:
            self._server_err.append((now, url))
            distinct = {u for t, u in self._server_err if now - t < 60}
        if len(distinct) >= 5:
            log.warning("Сайт отвечает ошибками 5xx на разные страницы — пауза 2 мин.")
            self.limiter.pause(120)
            with self._err_lock:
                self._server_err.clear()

    # ------------------------------------------------------------------ worker
    def process(self, task: Task) -> None:
        url = task.url
        stream = task.kind == "media"
        tmp = self._partial_path(url) if stream else None
        spill = None if stream else self._partial_path(url, ".spill")
        try:
            resp = self.fetcher.get(
                url,
                to_file=tmp,
                spill_file=spill,
                max_memory=self.cfg.max_page_size,
                max_size=self.cfg.max_file_size,
                etag=task.etag if task.path else None,
                last_modified=task.last_modified if task.path else None,
                stop=self.stop,
            )
            if resp.status in (404, 410) and task.alt_url and self.cfg.alias_fallback:
                log.debug("404 по %s, пробуем исходный адрес %s", url, task.alt_url)
                resp = self.fetcher.get(task.alt_url, to_file=tmp, spill_file=spill, max_memory=self.cfg.max_page_size,
                                        max_size=self.cfg.max_file_size, stop=self.stop)
        except FetchError as e:
            if spill:
                remove_partial(spill)
            self._handle_fetch_error(task, e, tmp)
            return
        self._handle_response(task, resp, tmp)

    def _handle_response(self, task: Task, resp, tmp: str | None) -> None:
        url = task.url
        st = resp.status
        if st in (301, 302, 303, 307, 308):
            if tmp:
                remove_partial(tmp)
            target = None
            if resp.location:
                norm = self.rules.normalize(resp.location, url)
                target = norm[0] if norm else None
                if target and self.cfg.rewrites:
                    target = self.cfg.rewrite(target)
            if target == url:
                # редирект «на себя» (например, выдача куки) — повторить ещё раз
                if task.tries < 2:
                    self.store.requeue(task.id, "редирект на себя", task.tries + 1, delay=5)
                else:
                    self.store.finish(task.id, status="redirect", http_status=st, location=target)
                return
            if target and task.kind == "media" and _AUTH_PATH.search(urlsplit(target).path):
                self.store.finish(task.id, status="auth", http_status=st, location=target,
                                  error="редирект на вход: нужны куки (cookies_file)")
                return
            self.store.finish(task.id, status="redirect", http_status=st, location=target)
            if target:
                kind = kind_by_ext(target) or (task.kind if task.kind != "sitemap" else "page")
                rows: list = []
                self._admit_into(rows, target, None, kind, task.depth, task.id, task.priority)
                self.store.add_urls(self._apply_query_caps(rows))
            return
        if st == 304:
            self.store.finish(task.id, status="done", http_status=304)
            return
        if st in (404, 410, 405):
            # 405 сервер azbyka.ru отдаёт на отсутствующие статические файлы
            if tmp:
                remove_partial(tmp)
            self.store.finish(task.id, status="notfound", http_status=st)
            return
        if st == 416:
            total = resp.extra.get("total")
            if tmp and os.path.exists(tmp) and total is not None and os.path.getsize(tmp) == total:
                # файл уже был докачан целиком до перезапуска — просто положить на место
                resp.status = 200
                resp.size = total
                resp.sha1 = file_sha1(tmp)
                rel = self._move_file(task, url, resp, tmp)
                self._done(task, resp, rel)
                return
            if tmp:
                remove_partial(tmp)
            self._retry_or_fail(task, "HTTP 416: докачка начнётся заново", st, delay=5)
            return
        if st in (429, 503) and resp.headers is not None and resp.headers.get("Retry-After"):
            wait = parse_retry_after(resp.headers.get("Retry-After"))
            if wait is None:
                wait = 60.0
            log.warning("HTTP %d на %s — сайт просит подождать %.0f с", st, url, wait)
            self.limiter.pause(wait)
            self._retry_or_fail(task, f"HTTP {st}", st, delay=wait)
            return
        if st == 429 or st >= 500:
            self._note_server_error(url)
            self._retry_or_fail(task, f"HTTP {st}", st)
            return
        if st in (401, 403):
            self._note_forbidden()
            if tmp:
                remove_partial(tmp)
            self._retry_or_fail(task, f"HTTP {st}", st)
            return
        if st not in (200, 203, 206):
            self._retry_or_fail(task, f"HTTP {st}", st)
            return
        ct = base_ctype(resp.ctype)
        path = tmp if tmp is not None else resp.tmp_path

        # Тело лежит в файле (тяжёлый файл или слишком большой/двоичный ответ)
        if path is not None:
            if ct in ("text/html", "application/xhtml+xml"):
                with open(path, "rb") as f:
                    head = f.read(self.cfg.max_page_size + 1)
                if not looks_binary(head):
                    if any(m in head for m in _LOGIN_MARKERS):
                        remove_partial(path)
                        self.store.finish(task.id, status="auth", http_status=st, content_type=resp.ctype,
                                          error="вместо файла страница входа: нужны куки (cookies_file)")
                        return
                    if is_challenge(head):
                        remove_partial(path)
                        log.warning("Анти-бот проверка вместо файла (%s) — пауза 10 мин", url)
                        self.limiter.pause(600)
                        self._retry_or_fail(task, "HTTP 503 анти-бот проверка", 503, spend=False, delay=600)
                        return
                    if len(head) <= self.cfg.max_page_size:
                        # это оказалась обычная страница — обработать как страницу
                        remove_partial(path)
                        if task.kind == "media":
                            self.store.execute("UPDATE urls SET kind='page' WHERE id=?", (task.id,))
                            task.kind = "page"
                        resp.body = head
                        self._finish_page(task, resp, head)
                        return
            rel = self._move_file(task, url, resp, path)
            self._done(task, resp, rel)
            return

        # Страница / ресурс в памяти
        body = resp.body or b""
        if ct in ("text/html", "application/xhtml+xml") and is_challenge(body):
            # страница-проверка анти-бот защиты вместо содержимого: не сохранять, переждать
            log.warning("Анти-бот проверка вместо страницы (%s) — пауза 10 мин", url)
            self.limiter.pause(600)
            self._retry_or_fail(task, "HTTP 503 анти-бот проверка", 503, spend=False, delay=600)
            return
        self._finish_page(task, resp, body)

    def _extract_and_enqueue(self, task: Task, url: str, ctype: str, body: bytes) -> tuple[str | None, int]:
        """Разбор сохранённого тела на ссылки. Возвращает (заголовок, новых адресов)."""
        ct = base_ctype(ctype)
        external = not self.rules.in_scope(url)
        up = urlsplit(url)
        if host_matches(up.hostname or "", self.cfg.peertube_hosts) and up.path.startswith("/api/"):
            found, title = peertube.handle_api(url, body, self.cfg.peertube_max_height)
            rows: list = []
            for u, k in found:
                self._admit_into(rows, u, None, k, task.depth + 1, task.id, task.priority)
            return title, self.store.add_urls(self._apply_query_caps(rows))
        should_parse = (ct in PARSE_CTYPES or task.kind == "sitemap"
                        or url_ext(url) in ("m3u", "m3u8")) and not self.cfg.no_follow(url)
        if external and ct != "text/css":
            should_parse = False
        if ct == "text/plain" and not url.lower().endswith("/robots.txt") and url_ext(url) not in ("m3u", "m3u8"):
            should_parse = False
        if not should_parse or len(body) > self.cfg.max_page_size:
            return None, 0
        data = maybe_gunzip(body, url, ctype or "")
        pctype = ctype or ""
        if data is not body:
            pctype = "application/xml"
        if url_ext(url) in ("m3u", "m3u8") and ct not in PARSE_CTYPES:
            pctype = "audio/x-mpegurl"
        links, meta = extract.extract_links(data, pctype, url)
        base = url
        if meta.get("base"):
            norm = self.rules.normalize(meta["base"], url)
            if norm:
                base = norm[0]
        n = self.enqueue_links(task, links, base, external_parent=external) if links else 0
        return meta.get("title") or None, n

    def _finish_page(self, task: Task, resp, body: bytes) -> None:
        url = task.url
        rel = self._write_page(task, url, resp, body)
        title, _ = self._extract_and_enqueue(task, url, resp.ctype, body)
        self._done(task, resp, rel, title)

    def _done(self, task: Task, resp, rel: str, title: str | None = None) -> None:
        h = resp.headers
        self.store.finish(
            task.id, status="done", http_status=resp.status, content_type=resp.ctype, size=resp.size,
            path=rel, sha1=resp.sha1, etag=h.get("ETag") if h else None,
            last_modified=h.get("Last-Modified") if h else None, title=title, error=None, next_try_at=0,
        )
        with self._stats_lock:
            self.stats["fetched"] += 1
            self.stats["bytes"] += resp.size or 0

    # ------------------------------------------------------------------ relink
    def relink(self, match: str = "", resume_key: str = "") -> int:
        """Повторно разобрать уже скачанные страницы по ТЕКУЩИМ правилам (без сети).

        Нужно после обновления программы/настроек: ссылки, которые раньше
        отсекались, попадут в очередь. С ``resume_key`` прогресс сохраняется,
        и прерванный разбор продолжается с того же места.
        """
        where = "status='done' AND path IS NOT NULL AND kind IN ('page','sitemap','asset')"
        params: tuple = ()
        if match:
            where += " AND url LIKE ?"
            params = (f"%{match}%",)
        start = int(self.store.get_meta(resume_key, "0") or 0) if resume_key else 0
        if start:
            log.info("relink продолжается с записи %d", start)
        n_pages = n_new = 0
        last_id = start
        cols = "id, url, alt_url, kind, priority, depth, tries, path, etag, last_modified, content_type"
        for row in self.store.iter_chunks(cols, where, params, start_id=start):
            if self.stop.is_set():
                break
            (tid, url, alt, kind, prio, depth, tries, path, etag, lm, ctype) = row
            last_id = tid
            ct = base_ctype(ctype)
            if (ct not in PARSE_CTYPES and kind != "sitemap" and url_ext(url) not in ("m3u", "m3u8")
                    and not (host_matches(urlsplit(url).hostname or "", self.cfg.peertube_hosts))):
                continue
            try:
                with open(mirror_file(self.mirror, path), "rb") as f:
                    body = f.read(self.cfg.max_page_size + 1)
                task = Task(tid, url, alt, kind, prio, depth, tries, path, etag, lm)
                _, n = self._extract_and_enqueue(task, url, ctype or "", body)
                n_new += n
            except OSError:
                continue
            except Exception:  # noqa: BLE001 — одна битая страница не должна срывать разбор
                log.exception("relink: сбой на %s", url)
                continue
            n_pages += 1
            if n_pages % 2000 == 0:
                self.flush_skips()
                if resume_key:
                    self.store.set_meta(resume_key, str(last_id))
                log.info("relink: разобрано %d страниц, новых адресов %d", n_pages, n_new)
        self.flush_skips()
        if resume_key:
            if self.stop.is_set():
                self.store.set_meta(resume_key, str(last_id))
            else:
                self.store.del_meta(resume_key)
        log.info("relink: страниц %d, новых адресов в очереди %d", n_pages, n_new)
        return n_new

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
        n = 0
        while not self.stop.is_set():
            try:
                task = None
                for kinds in lanes:
                    got = self.store.claim(kinds, 1)
                    if got:
                        task = got[0]
                        break
            except Exception:  # noqa: BLE001 — например, «database is locked»: подождать
                log.exception("ошибка базы при выборе задачи — повтор через 10 с")
                self.stop.wait(10)
                continue
            if task is None:
                self.stop.wait(1.0)
                continue
            with self._active_lock:
                self._active += 1
            try:
                n += 1
                if (task.kind == "media" or n % 50 == 0) and not self._disk_ok():
                    self.store.requeue(task.id, "", task.tries)
                    break
                self.process(task)
            except Exception as e:  # noqa: BLE001 — один плохой URL не должен ронять поток
                log.exception("сбой при обработке %s", task.url)
                try:
                    self._retry_or_fail(task, f"internal: {e!r}")
                except Exception:  # noqa: BLE001
                    log.exception("не удалось записать ошибку для %s", task.url)
                    self.stop.wait(5)
            finally:
                with self._active_lock:
                    self._active -= 1

    def run(self, progress_every: float = 15.0, max_seconds: float = 0) -> None:
        reset = self.store.reset_active()
        if reset:
            log.info("возвращено в очередь после прошлого запуска: %d", reset)
        # временные сбои прошлых сеансов (сеть, 5xx, 429, 403, обрывы) — повторить
        again = self.store.requeue_where(
            "status='error' AND (error LIKE 'network%' OR error LIKE 'HTTP 5%' OR error LIKE 'HTTP 4_9%'"
            " OR error LIKE 'HTTP 403%' OR error LIKE 'HTTP 401%' OR error LIKE 'обрыв%' OR error LIKE 'internal%'"
            " OR error LIKE 'tls%' OR error LIKE 'сервер вернул%')"
        )
        if again:
            log.info("повторяем адреса с временными ошибками прошлых сеансов: %d", again)
        self._sweep_partials()
        self.seed()
        threads = []
        for i in range(self.cfg.page_workers):
            threads.append(threading.Thread(target=self._worker, args=([PAGE_KINDS],), name=f"page-{i}", daemon=True))
        for i in range(self.cfg.media_workers):
            threads.append(threading.Thread(target=self._worker, args=([MEDIA_KINDS, PAGE_KINDS],),
                                            name=f"media-{i}", daemon=True))
        for t in threads:
            t.start()
        started = time.monotonic()
        last_bytes = last_fetched = 0
        last_t = last_ckpt = started
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
                deferred = self.store.query(
                    "SELECT COUNT(*) FROM urls WHERE status='queued' AND next_try_at > ?", (time.time(),)
                )[0][0]
                with self._active_lock:
                    active = self._active
                pause = self.limiter.paused_for()
                log.info(
                    "готово %d | в очереди %d%s | ошибок %d | нужен вход %d | за сеанс %s | %.1f стр/с | %s/с%s",
                    counts.get("done", 0), queued, f" (отложено {deferred})" if deferred else "",
                    counts.get("error", 0), counts.get("auth", 0), human_bytes(nbytes),
                    (fetched - last_fetched) / dt, human_bytes((nbytes - last_bytes) / dt),
                    f" | пауза {pause:.0f} с" if pause > 0 else "",
                )
                last_bytes, last_fetched, last_t = nbytes, fetched, now
                self.flush_skips()
                if now - last_ckpt > 300:
                    self.store.checkpoint()
                    last_ckpt = now
                if not any(t.is_alive() for t in threads):
                    self.stop_reason = self.stop_reason or "все рабочие потоки остановились"
                    break
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
            self.store.checkpoint()
            self.store.set_meta("last_run_end", str(time.time()))


def dates_nearest_first(d1: date, d2: date, center: date):
    """Даты отрезка [d1, d2], начиная с ближайших к ``center`` (сегодня):
    если сбор прервётся, ближайшие годы календаря уже будут сохранены."""
    if d1 > d2:
        return
    c = min(max(center, d1), d2)
    yield c
    one = timedelta(days=1)
    k = 1
    while True:
        fwd, back = c + k * one, c - k * one
        if fwd > d2 and back < d1:
            return
        if fwd <= d2:
            yield fwd
        if back >= d1:
            yield back
        k += 1
