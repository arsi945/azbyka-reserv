"""Извлечение ссылок из HTML, CSS, JSON/скриптов, M3U и XML-карт сайта."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .urls import ASSET_EXT, MEDIA_EXT, PAGE_EXT, kind_by_ext, url_ext


@dataclass(frozen=True)
class Link:
    url: str  # как в документе (может быть относительным)
    kind: str  # page | asset | media | embed
    source: str  # откуда взята (тег/атрибут), для отладки


_LINK_REL_ASSET = {
    "stylesheet", "icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed",
    "preload", "prefetch", "manifest", "mask-icon", "modulepreload", "image_src",
}
_LINK_REL_PAGE = {"canonical", "next", "prev", "alternate", "amphtml", "shortlink", "index", "first", "last", "up"}

_LAZY_ATTRS = (
    "data-src", "data-lazy-src", "data-original", "data-lazy", "data-bg", "data-background",
    "data-image", "data-img", "data-full", "data-large", "data-zoom-image", "data-thumb",
    "data-poster",
)
_LAZY_SRCSET = ("srcset", "data-srcset", "data-lazy-srcset")

_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+?)\1\s*\)""", re.I)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.I)
_ABS_URL_RE = re.compile(r"""https?://[^\s"'<>()\[\]{}\\^`|]+""", re.I)
_PROTO_REL_RE = re.compile(r"""(?<![:\w/])//(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?/[^\s"'<>()\[\]{}\\^`|]*""", re.I)
_QUOTED_PATH_RE = re.compile(r"""["'](/[^"'\s<>\\]{1,400})["']""")
_URLISH_RE = re.compile(r"""^(?:https?:)?//|^/[^/\s]|^\.{0,2}/?[\w\-./%~]+\.(\w{2,5})(?:[?#].*)?$""", re.I)
_META_REFRESH_RE = re.compile(r"""url\s*=\s*['"]?([^'";]+)""", re.I)
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w\-]+)""", re.I)
_SITEMAP_LOC_RE = re.compile(r"<(?:loc|image:loc|video:content_loc|video:player_loc)>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</", re.I | re.S)
_KNOWN_EXT = MEDIA_EXT | ASSET_EXT | PAGE_EXT


def guess_kind(url: str, default: str) -> str:
    k = kind_by_ext(url)
    return k or default


def parse_srcset(value: str) -> list[str]:
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(part.split()[0])
    return out


def css_links(text: str, source: str = "css") -> list[Link]:
    links = []
    for m in _CSS_URL_RE.finditer(text):
        u = m.group(2).strip()
        if u and not u.lower().startswith(("data:", "#")):
            links.append(Link(u, guess_kind(u, "asset"), source))
    for m in _CSS_IMPORT_RE.finditer(text):
        links.append(Link(m.group(2).strip(), "asset", source + "@import"))
    return links


def _unescape_js(text: str) -> str:
    return (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\x2F", "/")
        .replace("\\x2f", "/")
        .replace("&amp;", "&")
    )


# Куски JS-выражений и шаблонов, а не адреса: '/x/'+a+'', /wp-*.php, ${id}, {{url}}
_JS_JUNK = re.compile(r"""['"+*]|\$\{|\{\{|%27|%22|\+%27""")
_DIR_ONLY = re.compile(r"/wp-content/(themes|plugins)/[^/]+/?$")


def _plausible(u: str) -> bool:
    return not _JS_JUNK.search(u) and not _DIR_ONLY.search(u)


def text_links(text: str, source: str = "script") -> list[Link]:
    """URL из произвольного текста (JS, JSON, inline-конфиги плееров)."""
    text = _unescape_js(text)
    found: dict[str, Link] = {}
    for m in _ABS_URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:")
        if _plausible(u):
            found.setdefault(u, Link(u, guess_kind(u, "page"), source))
    for m in _PROTO_REL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:")
        if _plausible(u):
            found.setdefault(u, Link(u, guess_kind(u, "page"), source))
    for m in _QUOTED_PATH_RE.finditer(text):
        u = m.group(1)
        if url_ext(u) in _KNOWN_EXT and _plausible(u):
            found.setdefault(u, Link(u, guess_kind(u, "page"), source + ":path"))
    return list(found.values())


class _HTMLLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.base: str | None = None
        self.title_parts: list[str] = []
        self.canonical: str | None = None
        self.meta_robots: str = ""
        self._in_title = False
        self._raw_tag: str | None = None  # script / style
        self._raw_buf: list[str] = []
        self._media_stack: list[str] = []

    def _add(self, url: str | None, kind: str, source: str) -> None:
        if url:
            url = url.strip()
            if url and not url.startswith("#"):
                self.links.append(Link(url, kind, source))

    def handle_starttag(self, tag, attrs):  # noqa: C901 — простой диспетчер по тегам
        a: dict[str, str] = {}
        for k, v in attrs:
            if v is not None and k not in a:
                a[k.lower()] = v
        if tag == "base" and self.base is None and a.get("href"):
            self.base = a["href"].strip()
        elif tag in ("a", "area"):
            href = a.get("href")
            if href:
                self._add(href, guess_kind(href, "page"), tag)
        elif tag == "link":
            href = a.get("href")
            rels = set(a.get("rel", "").lower().split())
            if href:
                if rels & _LINK_REL_ASSET:
                    self._add(href, guess_kind(href, "asset"), "link:" + ",".join(sorted(rels)))
                elif rels & _LINK_REL_PAGE:
                    if "canonical" in rels:
                        self.canonical = href.strip()
                    self._add(href, guess_kind(href, "page"), "link:" + ",".join(sorted(rels)))
                else:
                    self._add(href, guess_kind(href, "asset"), "link")
        elif tag == "script":
            if a.get("src"):
                self._add(a["src"], "asset", "script")
            self._raw_tag = "script"
            self._raw_buf = []
        elif tag == "style":
            self._raw_tag = "style"
            self._raw_buf = []
        elif tag in ("audio", "video"):
            self._media_stack.append(tag)
            if a.get("src"):
                self._add(a["src"], guess_kind(a["src"], "media"), tag)
            if a.get("poster"):
                self._add(a["poster"], "asset", tag + ":poster")
        elif tag == "source":
            default = "media" if self._media_stack else "asset"
            if a.get("src"):
                self._add(a["src"], guess_kind(a["src"], default), "source")
            if a.get("srcset"):
                for u in parse_srcset(a["srcset"]):
                    self._add(u, "asset", "source:srcset")
        elif tag == "track":
            self._add(a.get("src"), "asset", "track")
        elif tag in ("iframe", "frame"):
            self._add(a.get("src"), "embed", tag)
        elif tag == "embed":
            if a.get("src"):
                self._add(a["src"], guess_kind(a["src"], "embed"), "embed")
        elif tag == "object":
            if a.get("data"):
                self._add(a["data"], guess_kind(a["data"], "embed"), "object")
        elif tag == "img" or (tag == "input" and a.get("type", "").lower() == "image"):
            self._add(a.get("src"), "asset", tag)
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content")
            if content:
                if prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src", "msapplication-tileimage"):
                    self._add(content, "asset", "meta:" + prop)
                elif prop in ("og:audio", "og:audio:url", "og:audio:secure_url", "og:video", "og:video:url", "og:video:secure_url"):
                    self._add(content, guess_kind(content, "media"), "meta:" + prop)
                elif prop == "robots":
                    self.meta_robots = content.lower()
            if a.get("http-equiv", "").lower() == "refresh" and content:
                m = _META_REFRESH_RE.search(content)
                if m:
                    self._add(m.group(1), "page", "meta:refresh")
        elif tag == "title":
            self._in_title = True
        elif tag == "body" and a.get("background"):
            self._add(a["background"], "asset", "body:background")
        elif tag in ("td", "table", "th") and a.get("background"):
            self._add(a["background"], "asset", tag + ":background")

        # общие атрибуты
        for attr in _LAZY_SRCSET:
            if attr in a and (attr != "srcset" or tag not in ("source",)):
                for u in parse_srcset(a[attr]):
                    self._add(u, "asset", f"{tag}:{attr}")
        for attr in _LAZY_ATTRS:
            if attr in a:
                self._add(a[attr], guess_kind(a[attr], "asset"), f"{tag}:{attr}")
        if "style" in a and "url(" in a["style"]:
            for link in css_links(a["style"], f"{tag}:style"):
                self.links.append(link)
        # прочие data-* атрибуты, похожие на ссылки (data-href, data-url, data-file, data-mp3 ...)
        for k, v in a.items():
            if k.startswith("data-") and k not in _LAZY_ATTRS and k not in _LAZY_SRCSET:
                v = v.strip()
                if 0 < len(v) < 500 and " " not in v and _URLISH_RE.search(v):
                    if v.startswith(("http://", "https://", "//", "/")) or url_ext(v) in _KNOWN_EXT:
                        self._add(v, guess_kind(v, "page"), f"{tag}:{k}")

    def handle_endtag(self, tag):
        if tag in ("audio", "video") and self._media_stack:
            self._media_stack.pop()
        elif tag == "title":
            self._in_title = False
        elif self._raw_tag and tag == self._raw_tag:
            text = "".join(self._raw_buf)
            if self._raw_tag == "style":
                self.links.extend(css_links(text, "style"))
            else:
                self.links.extend(text_links(text, "script"))
            self._raw_tag = None
            self._raw_buf = []

    def handle_data(self, data):
        if self._raw_tag:
            self._raw_buf.append(data)
        elif self._in_title:
            self.title_parts.append(data)


@dataclass
class HtmlInfo:
    links: list[Link]
    base: str | None
    title: str
    canonical: str | None
    meta_robots: str


def html_links(text: str) -> HtmlInfo:
    p = _HTMLLinks()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 — битый HTML не должен ронять обход
        pass
    title = re.sub(r"\s+", " ", "".join(p.title_parts)).strip()
    return HtmlInfo(p.links, p.base, title, p.canonical, p.meta_robots)


def sitemap_links(text: str) -> list[Link]:
    out = []
    for m in _SITEMAP_LOC_RE.finditer(text):
        u = m.group(1).strip().replace("&amp;", "&")
        if u:
            kind = "sitemap" if u.lower().split("?")[0].endswith((".xml", ".xml.gz")) else guess_kind(u, "page")
            out.append(Link(u, kind, "sitemap"))
    return out


def m3u_links(text: str) -> list[Link]:
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line and not line.startswith("#"):
            out.append(Link(line, guess_kind(line, "media"), "m3u"))
    return out


def robots_sitemaps(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if k.strip().lower() == "sitemap" and v.strip():
            out.append(v.strip())
    return out


def sniff_charset(body: bytes, header_ctype: str | None) -> str:
    if header_ctype:
        m = re.search(r"charset=([\w\-]+)", header_ctype, re.I)
        if m:
            return m.group(1)
    m = _CHARSET_RE.search(body[:4096])
    if m:
        return m.group(1).decode("ascii", "replace")
    return "utf-8"


def decode_body(body: bytes, header_ctype: str | None) -> str:
    cs = sniff_charset(body, header_ctype)
    try:
        return body.decode(cs, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_links(body: bytes, ctype: str, url: str) -> tuple[list[Link], dict]:
    """Ссылки по типу содержимого. Возвращает (ссылки, метаданные)."""
    base_ct = (ctype or "").split(";", 1)[0].strip().lower()
    meta: dict = {}
    if base_ct in ("text/html", "application/xhtml+xml"):
        info = html_links(decode_body(body, ctype))
        meta = {"title": info.title, "base": info.base, "canonical": info.canonical, "meta_robots": info.meta_robots}
        return info.links, meta
    text = decode_body(body, ctype)
    if base_ct == "text/css":
        return css_links(text), meta
    if base_ct in ("application/xml", "text/xml") or url.lower().split("?")[0].endswith(".xml"):
        if "<urlset" in text[:2000] or "<sitemapindex" in text[:2000]:
            return sitemap_links(text), meta
        return text_links(text, "xml"), meta
    if base_ct in ("audio/x-mpegurl", "audio/mpegurl", "application/vnd.apple.mpegurl", "application/x-mpegurl") or url_ext(url) in ("m3u", "m3u8"):
        return m3u_links(text), meta
    if base_ct in ("application/json", "application/ld+json", "application/javascript", "text/javascript", "application/x-javascript"):
        return text_links(text, base_ct.split("/")[-1]), meta
    if base_ct == "text/plain" and url.lower().endswith("/robots.txt"):
        return [Link(u, "sitemap", "robots") for u in robots_sitemaps(text)], meta
    return [], meta
