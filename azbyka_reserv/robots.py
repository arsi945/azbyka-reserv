"""Разбор robots.txt с поддержкой ``*``, ``$``, Allow/Disallow (правило
самого длинного совпадения, как у Google/Яндекса), Crawl-delay и Clean-param.

Стандартный ``urllib.robotparser`` не понимает шаблоны ``*`` и ``$``,
поэтому здесь своя небольшая реализация.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str, re.Pattern]] = field(default_factory=list)  # (allow, raw, regex)
    crawl_delay: float | None = None


def _compile(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    parts = [re.escape(p) for p in path.split("*")]
    rx = ".*".join(parts)
    return re.compile("^" + rx + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str = "", agent: str = "azbyka-reserv") -> None:
        self.agent = agent.lower()
        self.groups: list[_Group] = []
        self.clean_params: list[tuple[list[str], str]] = []  # (params, path_prefix)
        self.sitemaps: list[str] = []
        self.raw = text
        self._parse(text)
        self._group = self._select_group()

    def _parse(self, text: str) -> None:
        cur: _Group | None = None
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if cur is None or not last_was_agent:
                    cur = _Group()
                    self.groups.append(cur)
                cur.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key == "sitemap":
                self.sitemaps.append(value)
            elif key == "clean-param":
                params, _, prefix = value.partition(" ")
                self.clean_params.append(([p for p in params.split("&") if p], prefix.strip() or "/"))
            elif cur is None:
                continue
            elif key in ("allow", "disallow"):
                if not value:
                    # пустой Disallow = всё разрешено
                    continue
                cur.rules.append((key == "allow", value, _compile(value)))
            elif key == "crawl-delay":
                try:
                    cur.crawl_delay = float(value.replace(",", "."))
                except ValueError:
                    pass

    def _select_group(self) -> _Group | None:
        star = None
        for g in self.groups:
            for a in g.agents:
                if a != "*" and a in self.agent:
                    return g
                if a == "*":
                    star = star or g
        return star

    @property
    def crawl_delay(self) -> float | None:
        return self._group.crawl_delay if self._group else None

    def allowed(self, url: str) -> bool:
        if not self._group:
            return True
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        candidates = {target, unquote(target)}
        best_len = -1
        best_allow = True
        for allow, raw, rx in self._group.rules:
            if any(rx.match(t) for t in candidates):
                ln = len(raw)
                if ln > best_len or (ln == best_len and allow):
                    best_len = ln
                    best_allow = allow
        return best_allow

    def params_to_clean(self, url: str) -> list[str]:
        path = urlsplit(url).path
        out: list[str] = []
        for params, prefix in self.clean_params:
            if path.startswith(prefix) or _compile(prefix).match(path):
                out.extend(params)
        return out
