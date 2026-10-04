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
        self.seed_date_templates: list[str] = crawl.get("seed_date_templates", [])
        self.pause_on_network_error: float = float(crawl.get("pause_on_network_error", 300))

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

    def date_filtered(self, url: str) -> bool:
        """True — URL календарного типа с датой вне [date_from, date_to]."""
        if not any(rx.search(url) for rx in self.date_filter_rx):
            return False
        for m in _DATE_RE.finditer(url):
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if d < self.date_from or d > self.date_to:
                return True
        for m in _YEAR_SEG_RE.finditer(url):
            y = int(m.group(1))
            if y < self.date_from.year or y > self.date_to.year:
                return True
        return False


_DATE_RE = re.compile(r"(?<!\d)((?:1[6-9]|2\d)\d\d)[-./]?(0[1-9]|1[0-2])[-./]?(0[1-9]|[12]\d|3[01])(?!\d)")
_YEAR_SEG_RE = re.compile(r"[/=]((?:1[6-9]|2\d)\d\d)(?=/|$|&|\?)")


def load_config(user_path: str | None = None, overrides: dict[str, Any] | None = None) -> Config:
    raw = load_raw(user_path)
    if overrides:
        raw = _merge(raw, overrides)
    return Config(raw)
