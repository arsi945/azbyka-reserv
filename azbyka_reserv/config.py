"""Загрузка конфигурации: встроенные значения по умолчанию + файл пользователя.

Пользовательский TOML накладывается поверх ``default_config.toml``:
таблицы сливаются, списки заменяются. Ключ с префиксом ``add_``
(например ``add_exclude``) не заменяет, а дополняет одноимённый список.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import unquote

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.toml")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k.startswith("add_") and isinstance(v, list):
            key = k[4:]
            out[key] = list(out.get(key, [])) + v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_raw(user_path: str | None) -> dict:
    with open(DEFAULT_CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    if user_path:
        with open(user_path, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    return cfg


@dataclass
class PatternValue:
    rx: re.Pattern
    value: int


@dataclass
class Variant:
    """Для URL, совпавшего с ``rx``, поставить в очередь ещё ``template`` для
    каждого значения из ``values`` ({1}, {2}… — группы, {x} — значение)."""

    rx: re.Pattern
    template: str
    values: list[str]
    priority: int | None


@dataclass
class Config:
    raw: dict

    def __post_init__(self) -> None:
        r = self.raw
        site, crawl, rules = r["site"], r["crawl"], r["rules"]
        self.start_urls: list[str] = site["start_urls"]
        self.scope_hosts: list[str] = site["scope_hosts"]
        self.host_aliases: dict[str, str] = site.get("host_aliases", {})
        self.alias_fallback: bool = site.get("alias_fallback", True)
        self.https_hosts: list[str] = site.get("https_hosts", [])
        self.drop_params: list[str] = site.get("drop_params", [])
        self.extra_media_hosts: list[str] = site.get("extra_media_hosts", [])
        self.sitemap_urls: list[str] = site.get("sitemap_urls", [])

        self.page_workers: int = int(crawl["page_workers"])
        self.media_workers: int = int(crawl["media_workers"])
        self.min_delay: float = float(crawl["min_delay"])
        self.timeout: float = float(crawl["timeout"])
        self.max_tries: int = int(crawl["max_tries"])
        self.max_depth: int = int(crawl.get("max_depth", 0))
        self.respect_robots: bool = bool(crawl["respect_robots"])
        # "pages" — robots.txt применяется к страницам, но не к файлам для
        # скачивания (mp3/epub/pdf…), которые сайт закрывает только от индексации;
        # "all" — ко всему.
        self.robots_scope: str = str(crawl.get("robots_scope", "pages"))
        # Clean-param из robots.txt: у azbyka.ru он ломает /worships/?…&worship=…
        self.apply_clean_param: bool = bool(crawl.get("apply_clean_param", False))
        self.robots_override_rx = [re.compile(p) for p in crawl.get("robots_override", [])]
        self.login_required_rx = [re.compile(p) for p in crawl.get("login_required", [])]
        self.user_agent: str = crawl["user_agent"]
        self.max_file_size: int = int(float(crawl.get("max_file_size_mb", 0)) * 1024 * 1024)
        self.max_page_size: int = int(float(crawl.get("max_page_size_mb", 30)) * 1024 * 1024)
        self.min_free_bytes: int = int(float(crawl.get("min_free_gb", 5)) * 1024**3)
        self.bandwidth_limit: int = int(float(crawl.get("bandwidth_limit_kbps", 0)) * 1024)
        self.default_query_cap: int = int(crawl.get("query_variants_cap", 300))
        self.external_assets: bool = bool(crawl.get("external_assets", True))
        self.asset_host_blocklist: list[str] = crawl.get("asset_host_blocklist", [])
        self.embed_hosts: list[str] = crawl.get("embed_hosts", [])
        self.cookies_file: str = crawl.get("cookies_file", "")
        self.date_from: date = date.fromisoformat(crawl.get("date_from", "2000-01-01"))
        self.date_to: date = date.fromisoformat(crawl.get("date_to", "2045-12-31"))
        self.date_filter_rx = [re.compile(p) for p in crawl.get("date_filter_patterns", [])]
        self.seed_days: bool = bool(crawl.get("seed_days", True))
        self.seed_days_template: str = crawl.get("seed_days_template", "https://azbyka.ru/days/{date}")
        # элементы: строка-шаблон или таблица {template, from, to, priority}
        self.seed_date_templates: list = crawl.get("seed_date_templates", [])
        self.pause_on_network_error: float = float(crawl.get("pause_on_network_error", 300))
        # первый повтор неудачного адреса — через N секунд, дальше вдвое дольше (до часа)
        self.retry_backoff: float = float(crawl.get("retry_backoff", 30))

        pt = r.get("peertube", {})
        self.peertube_hosts: list[str] = pt.get("hosts", [])
        self.peertube_max_height: int = int(pt.get("max_height", 720))
        self.peertube_seed_listing: bool = bool(pt.get("seed_listing", True))

        self.exclude_rx = [re.compile(p) for p in rules.get("exclude", [])]
        self.include_rx = [re.compile(p) for p in rules.get("include_override", [])]
        self.no_follow_rx = [re.compile(p) for p in rules.get("no_follow", [])]
        self.priority = [PatternValue(re.compile(x["pattern"]), int(x["value"])) for x in r.get("priority", [])]
        self.query_caps = [PatternValue(re.compile(x["pattern"]), int(x["value"])) for x in r.get("query_cap", [])]
        self.default_priority: int = int(rules.get("default_priority", 50))
        self.rewrites = [(re.compile(x["pattern"]), x["replace"]) for x in r.get("rewrite", [])]
        self.variants = [
            Variant(re.compile(x["pattern"]), x["template"], list(x["values"]),
                    int(x["priority"]) if "priority" in x else None)
            for x in r.get("variants", [])
        ]

    # -- правила ---------------------------------------------------------------
    # Канонические URL хранят кириллицу в %-кодировке, а в правилах удобнее
    # писать её как есть. Поэтому каждое правило проверяется на обеих формах.
    @staticmethod
    def _forms(url: str) -> tuple[str, ...]:
        dec = unquote(url, errors="replace")
        return (url,) if dec == url else (url, dec)

    @staticmethod
    def _hit(rx: re.Pattern, forms: tuple[str, ...]) -> bool:
        return any(rx.search(f) for f in forms)

    def excluded(self, url: str) -> bool:
        forms = self._forms(url)
        if any(self._hit(rx, forms) for rx in self.include_rx):
            return False
        return any(self._hit(rx, forms) for rx in self.exclude_rx)

    def no_follow(self, url: str) -> bool:
        forms = self._forms(url)
        return any(self._hit(rx, forms) for rx in self.no_follow_rx)

    def priority_for(self, url: str) -> int | None:
        forms = self._forms(url)
        for pv in self.priority:
            if self._hit(pv.rx, forms):
                return pv.value
        return None

    def query_cap_for(self, url: str) -> int:
        forms = self._forms(url)
        for pv in self.query_caps:
            if self._hit(pv.rx, forms):
                return pv.value
        return self.default_query_cap

    def robots_overridden(self, url: str) -> bool:
        return any(rx.search(url) for rx in self.robots_override_rx)

    def needs_login(self, url: str) -> bool:
        forms = self._forms(url)
        return any(self._hit(rx, forms) for rx in self.login_required_rx)

    def rewrite(self, url: str) -> str:
        """Первое подходящее правило [[rewrite]] (например, стих Библии -> глава)."""
        for rx, rep in self.rewrites:
            if rx.search(url):
                return rx.sub(rep, url, count=1)
        return url

    def date_filtered(self, url: str) -> bool:
        """True — календарный URL с датой вне [date_from, date_to].

        Учитываются только явные даты (ГГГГ-ММ-ДД в пути или ?date=…) и год
        календаря (/calendar/ГГГГ). Четырёхзначные номера папок (иконы
        /icons-of-saints/1851/…) датами не считаются.
        """
        if not any(rx.search(url) for rx in self.date_filter_rx):
            return False
        for m in _DATE_RE.finditer(url):
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if d < self.date_from or d > self.date_to:
                return True
        for m in _CAL_YEAR_RE.finditer(url):
            y = int(m.group(1))
            if y < self.date_from.year or y > self.date_to.year:
                return True
        return False

    def admission_signature(self) -> str:
        """Отпечаток настроек, от которых зависит, какие ссылки попадут в очередь.

        Скорость, потоки, таймауты и т.п. сюда не входят: их изменение не
        требует заново разбирать уже скачанные страницы.
        """
        import hashlib
        import json

        r = self.raw
        crawl = r.get("crawl", {})
        keys = ("respect_robots", "robots_scope", "robots_override", "apply_clean_param", "login_required",
                "date_from", "date_to", "date_filter_patterns", "external_assets", "asset_host_blocklist",
                "embed_hosts", "max_depth", "query_variants_cap")
        sub = {
            "site": {k: v for k, v in r.get("site", {}).items() if k not in ("start_urls", "sitemap_urls")},
            "crawl": {k: crawl.get(k) for k in keys},
            "has_cookies": bool(crawl.get("cookies_file")),
            "rules": r.get("rules"), "priority": r.get("priority"), "query_cap": r.get("query_cap"),
            "rewrite": r.get("rewrite"), "variants": r.get("variants"), "peertube": r.get("peertube"),
        }
        return hashlib.sha1(json.dumps(sub, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


_DATE_RE = re.compile(
    r"(?:(?<=/)|(?<=[?&]date=))((?:1[6-9]|2\d)\d\d)-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?![\d])"
)
_CAL_YEAR_RE = re.compile(r"/calendar/((?:1[6-9]|2\d)\d\d)(?=/|$|\?)")


def load_config(user_path: str | None = None, overrides: dict[str, Any] | None = None) -> Config:
    raw = load_raw(user_path)
    if overrides:
        raw = _merge(raw, overrides)
    return Config(raw)
