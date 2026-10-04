"""Нормализация URL, проверка области обхода и отображение URL -> файл."""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import string
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

_HEX = set("0123456789abcdefABCDEF")
_UNRESERVED = set(string.ascii_letters + string.digits + "-._~")
_SUB_DELIMS = set("!$&'()*+,;=")
_PATH_SAFE = _UNRESERVED | _SUB_DELIMS | set(":@/")
_QUERY_SAFE = _PATH_SAFE | set("?")

DEFAULT_PORTS = {"http": 80, "https": 443}


def _norm_pct(s: str, safe: set[str]) -> str:
    """Нормализует процентное кодирование по RFC 3986.

    * ``%xx`` для незарезервированных ASCII-символов раскодируются;
    * остальные ``%xx`` приводятся к верхнему регистру;
    * «сырые» символы вне ``safe`` (кириллица, пробелы...) кодируются в UTF-8.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "%":
            if i + 2 < n and s[i + 1] in _HEX and s[i + 2] in _HEX:
                byte = int(s[i + 1 : i + 3], 16)
                c = chr(byte)
                if byte < 128 and c in _UNRESERVED:
                    out.append(c)
                else:
                    out.append("%" + s[i + 1 : i + 3].upper())
                i += 3
                continue
            out.append("%25")
            i += 1
            continue
        if ch in safe:
            out.append(ch)
        else:
            for b in ch.encode("utf-8", "surrogatepass"):
                out.append("%%%02X" % b)
        i += 1
    return "".join(out)


def remove_dot_segments(path: str) -> str:
    """RFC 3986, 5.2.4."""
    if "." not in path:
        return path
    segs = path.split("/")
    out: list[str] = []
    for seg in segs:
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(seg)
    res = "/".join(out)
    if segs and segs[-1] in (".", ".."):
        res += "/"
    if path.startswith("/") and not res.startswith("/"):
        res = "/" + res
    return res


@dataclass
class UrlRules:
    """Правила нормализации и области обхода (из конфига)."""

    scope_hosts: list[str] = field(default_factory=lambda: ["azbyka.ru"])
    # хост -> канонический хост (например azbyka.org -> azbyka.ru)
    host_aliases: dict[str, str] = field(default_factory=dict)
    # параметры запроса, которые выбрасываются (точное имя или префикс с '*')
    drop_params: list[str] = field(default_factory=list)
    # хосты, для которых схема всегда https
    https_hosts: list[str] = field(default_factory=lambda: ["azbyka.ru"])

    def __post_init__(self) -> None:
        self._drop_exact = {p.lower() for p in self.drop_params if not p.endswith("*")}
        self._drop_prefix = tuple(p[:-1].lower() for p in self.drop_params if p.endswith("*"))
        self._scope = [h.lower().lstrip(".") for h in self.scope_hosts]
        self._aliases = {k.lower(): v.lower() for k, v in self.host_aliases.items()}
        self._https = {h.lower() for h in self.https_hosts}

    # -- область -----------------------------------------------------------
    def host_in_scope(self, host: str) -> bool:
        host = host.lower()
        for h in self._scope:
            if host == h or host.endswith("." + h):
                return True
        return False

    def in_scope(self, url: str) -> bool:
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            return False
        return self.host_in_scope(host)

    # -- нормализация ------------------------------------------------------
    def _keep_param(self, token: str) -> bool:
        key = unquote(token.split("=", 1)[0]).lower()
        if key in self._drop_exact:
            return False
        if self._drop_prefix and key.startswith(self._drop_prefix):
            return False
        return True

    def normalize(self, url: str, base: str | None = None) -> tuple[str, str | None] | None:
        """Возвращает ``(канонический_url, исходный_url_если_был_алиас)`` или None.

        None означает, что ссылка не HTTP(S) или испорчена.
        """
        url = url.strip()
        if not url:
            return None
        # убрать переводы строк и табы внутри ссылки (встречаются в HTML)
        url = re.sub(r"[\t\r\n]", "", url)
        low = url[:12].lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "data:", "about:", "blob:", "#")):
            return None
        try:
            if base is not None:
                url = urljoin(base, url)
            parts = urlsplit(url)
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        try:
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port
        except ValueError:
            return None
        if not host:
            return None
        try:
            host = host.encode("idna").decode("ascii") if not host.isascii() else host
        except UnicodeError:
            return None
        original: str | None = None
        canon_host = self._aliases.get(host)
        if canon_host and canon_host != host:
            original_host = host
            host = canon_host
            port = None
        else:
            original_host = None
        if host in self._https or any(host.endswith("." + h) for h in self._https):
            scheme = "https"
        netloc = host
        if port and port != DEFAULT_PORTS.get(scheme):
            netloc = f"{host}:{port}"
        path = _norm_pct(parts.path or "/", _PATH_SAFE)
        path = remove_dot_segments(path)
        if not path.startswith("/"):
            path = "/" + path
        query = parts.query
        if query:
            tokens = [t for t in query.split("&") if t]
            # мусорные параметры убираем только у «своих» хостов: у чужих
            # (youtube.com/watch?v=…) они могут быть значимыми
            own = self.host_in_scope(host)
            tokens = [_norm_pct(t, _QUERY_SAFE) for t in tokens if not own or self._keep_param(t)]
            query = "&".join(tokens)
        canon = urlunsplit((scheme, netloc, path, query, ""))
        if original_host is not None:
            o_netloc = original_host
            if parts.port and parts.port != DEFAULT_PORTS.get(parts.scheme.lower()):
                o_netloc = f"{original_host}:{parts.port}"
            original = urlunsplit((parts.scheme.lower(), o_netloc, path, query, ""))
        return canon, original


# --- классификация по расширению -------------------------------------------

MEDIA_EXT = {
    # аудио
    "mp3", "m4a", "m4b", "ogg", "oga", "opus", "wav", "flac", "aac", "wma", "amr",
    # видео
    "mp4", "m4v", "webm", "mkv", "avi", "mov", "flv", "3gp", "mpg", "mpeg", "wmv", "ts",
    # книги и документы
    "pdf", "epub", "fb2", "mobi", "azw3", "djvu", "djv", "doc", "docx", "rtf", "odt",
    "txt", "xls", "xlsx", "ppt", "pptx", "odp", "ods", "chm",
    # архивы
    "zip", "rar", "7z", "gz", "tgz", "bz2", "xz",
    # ноты
    "mid", "midi", "mus", "musx", "sib", "nwc", "xml.mxl", "mxl",
}
ASSET_EXT = {
    "css", "js", "mjs", "json", "map",
    "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico", "tif", "tiff", "avif", "jfif",
    "woff", "woff2", "ttf", "otf", "eot",
    "m3u", "m3u8", "pls", "vtt", "srt",
}
PAGE_EXT = {"html", "htm", "shtml", "php", "asp", "aspx", "jsp", "xhtml"}


def url_ext(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".fb2.zip"):
        return "fb2.zip"
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1]


def kind_by_ext(url: str) -> str | None:
    ext = url_ext(url)
    if not ext:
        return None
    if ext in MEDIA_EXT or ext == "fb2.zip":
        return "media"
    if ext in ASSET_EXT:
        return "asset"
    if ext in PAGE_EXT:
        return "page"
    return None


# --- отображение URL -> относительный путь файла ---------------------------

_CTYPE_EXT = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "application/ld+json": ".json",
    "text/plain": ".txt",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/rss+xml": ".xml",
    "application/atom+xml": ".xml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4b": ".m4b",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/x-mpegurl": ".m3u",
    "audio/mpegurl": ".m3u",
    "application/vnd.apple.mpegurl": ".m3u8",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "application/x-fictionbook+xml": ".fb2",
    "application/x-fictionbook": ".fb2",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/msword": ".doc",
    "application/rtf": ".rtf",
    "image/vnd.djvu": ".djvu",
    "image/x-djvu": ".djvu",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/vnd.ms-fontobject": ".eot",
}


def ext_for_ctype(ctype: str | None) -> str:
    if not ctype:
        return ".bin"
    base = ctype.split(";", 1)[0].strip().lower()
    if base in _CTYPE_EXT:
        return _CTYPE_EXT[base]
    guess = mimetypes.guess_extension(base)
    return guess or ".bin"


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f\x7f]')
MAX_SEGMENT = 100  # символов в одном имени
MAX_RELPATH = 220  # символов во всём относительном пути (Windows MAX_PATH = 260)


def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:n]


def safe_segment(seg: str) -> str:
    """Делает из сегмента URL безопасное имя файла/папки для Windows/Linux/macOS."""
    seg = unquote(seg, errors="replace")
    seg = _BAD_CHARS.sub("_", seg).replace("/", "_")
    if seg in ("", ".", ".."):
        seg = "_" + seg
    seg = seg.rstrip(" .") or "_"
    if seg.split(".", 1)[0].upper() in _WIN_RESERVED:
        seg = "_" + seg
    if len(seg) > MAX_SEGMENT:
        stem, dot, ext = seg.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            seg = stem[: MAX_SEGMENT - 18] + "~" + _short_hash(seg) + "." + ext
        else:
            seg = seg[: MAX_SEGMENT - 9] + "~" + _short_hash(seg)
    return seg


def _split_name(name: str) -> tuple[str, str]:
    if "." in name.lstrip("."):
        stem, ext = name.rsplit(".", 1)
        if 0 < len(ext) <= 8 and ext.isalnum():
            return stem, "." + ext
    return name, ""


def url_to_relpath(url: str, ctype: str | None = None, filename: str | None = None) -> str:
    """Относительный POSIX-путь файла внутри папки ``mirror``.

    ``filename`` — имя из заголовка Content-Disposition (если было).
    Уникальность (в т.ч. без учёта регистра) обеспечивает вызывающий код
    через :func:`disambiguate`.
    """
    parts = urlsplit(url)
    host = parts.hostname or "_"
    if parts.port:
        host = f"{host}_{parts.port}"
    raw_segs = parts.path.split("/")[1:]  # путь всегда начинается с '/'
    trailing = parts.path.endswith("/")
    if trailing:
        raw_segs = raw_segs[:-1]
    segs = [safe_segment(s) for s in raw_segs]
    ctype_ext = ext_for_ctype(ctype) if ctype else ""
    is_html = ctype_ext == ".html"

    if filename:
        fname = safe_segment(filename)
        dirs = segs
    elif trailing or not segs:
        dirs = segs
        fname = "index" + (ctype_ext or ".html")
    else:
        last = segs[-1]
        stem, ext = _split_name(last)
        if ext:
            dirs = segs[:-1]
            if is_html and ext.lower() not in (".html", ".htm"):
                fname = last + ".html"
            else:
                fname = last
        else:
            # «папочный» URL без расширения: /drevo-zhizni -> drevo-zhizni/index.html
            dirs = segs
            fname = "index" + (ctype_ext or ".html")

    if parts.query:
        stem, ext = _split_name(fname)
        fname = f"{stem}@{_short_hash(parts.query)}{ext}"

    rel = "/".join([safe_segment(host)] + dirs + [fname])
    if len(rel) > MAX_RELPATH:
        # уникальность даёт папка-хэш ниже, имя файла оставляем читаемым
        stem, ext = _split_name(fname)
        if len(fname) > 80:
            fname = stem[:70] + ext
        keep: list[str] = []
        budget = MAX_RELPATH - len(fname) - len(host) - 30
        for d in dirs:
            if len("/".join(keep + [d])) > budget:
                break
            keep.append(d)
        rel = "/".join([safe_segment(host)] + keep + ["_long", _short_hash(url, 12), fname])
    return rel


def disambiguate(relpath: str, salt: str) -> str:
    """Добавляет к имени файла хэш, если путь уже занят другим URL."""
    head, _, name = relpath.rpartition("/")
    stem, ext = _split_name(name)
    name = f"{stem}~{_short_hash(salt, 6)}{ext}"
    return f"{head}/{name}" if head else name


def path_prefix(url: str) -> str:
    """Первый сегмент пути: '/otechnik/...' -> 'otechnik' (для отчётов)."""
    p = urlsplit(url).path.strip("/")
    return p.split("/", 1)[0] if p else ""


__all__ = [
    "UrlRules",
    "url_ext",
    "kind_by_ext",
    "url_to_relpath",
    "disambiguate",
    "ext_for_ctype",
    "safe_segment",
    "path_prefix",
    "remove_dot_segments",
]
