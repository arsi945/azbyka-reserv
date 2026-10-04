"""Видео с собственного PeerTube портала (tube.azbyka.ru).

Плеер встраивается iframe-ом ``/videos/embed/<id>``, сам ролик грузится
скриптом, поэтому обычный обход видеофайлов не находит. Здесь:

* ссылки на ролики (``/videos/embed/<id>``, ``/w/<id>``, ``/videos/watch/<id>``)
  превращаются в запрос API ``/api/v1/videos/<id>``;
* из ответа API выбирается ОДИН файл нужного качества (а не все разрешения
  и HLS-фрагменты) + превью;
* список всех роликов (``/api/v1/videos?start=..``) обходится постранично,
  чтобы взять и те, что не встроены ни в одну страницу.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_VIDEO_PATH = re.compile(r"^/(?:videos/(?:embed|watch)|w)/([0-9A-Za-z-]{6,})/?$")
_API_VIDEO = re.compile(r"^/api/v1/videos/([0-9A-Za-z-]{6,})/?$")
_ALLOWED = re.compile(r"^/(api/v1/videos(/[0-9A-Za-z-]+)?/?$|static/|lazy-static/|download/videos/|download/streaming-playlists/)")
PAGE_SIZE = 50


def video_id(path: str) -> str | None:
    m = _VIDEO_PATH.match(path) or _API_VIDEO.match(path)
    return m.group(1) if m else None


def api_url(origin: str, vid: str) -> str:
    return f"{origin}/api/v1/videos/{vid}"


def list_url(origin: str, start: int = 0) -> str:
    q = urlencode({"start": start, "count": PAGE_SIZE, "sort": "publishedAt", "isLocal": "true"})
    return f"{origin}/api/v1/videos?{q}"


def map_url(url: str) -> tuple[str, str] | None:
    """URL на PeerTube -> (url_для_очереди, kind) или None (не нужен)."""
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    vid = video_id(p.path)
    if vid and not p.path.startswith("/api/"):
        return api_url(origin, vid), "page"
    if _ALLOWED.match(p.path):
        if p.path.startswith("/api/"):
            return url, "page"
        if p.path.startswith(("/download/", "/static/web-videos/", "/static/webseed/", "/static/streaming-playlists/")):
            return url, "media"
        return url, "asset"
    return None


def _height(f: dict) -> int:
    r = f.get("resolution") or {}
    try:
        return int(r.get("id") if isinstance(r, dict) else r)
    except (TypeError, ValueError):
        return 0


def choose_file(video: dict, max_height: int) -> dict | None:
    files = list(video.get("files") or [])
    for pl in video.get("streamingPlaylists") or []:
        files.extend(pl.get("files") or [])
    files = [f for f in files if (f.get("fileDownloadUrl") or f.get("fileUrl")) and _height(f) > 0]
    if not files:
        return None
    fit = [f for f in files if _height(f) <= max_height]
    if fit:
        best = max(_height(f) for f in fit)
        cands = [f for f in fit if _height(f) == best]
    else:
        low = min(_height(f) for f in files)
        cands = [f for f in files if _height(f) == low]
    # обычный mp4 (files) предпочтительнее HLS-варианта — он первым в списке
    return cands[0]


def handle_api(url: str, body: bytes, max_height: int) -> tuple[list[tuple[str, str]], str | None]:
    """Разбор ответа API. Возвращает ([(url, kind)], заголовок)."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return [], None
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    out: list[tuple[str, str]] = []
    if isinstance(data, dict) and "data" in data and "total" in data:
        for v in data.get("data") or []:
            vid = v.get("uuid") or v.get("shortUUID")
            if vid:
                out.append((api_url(origin, vid), "page"))
        q = dict(parse_qsl(p.query))
        start = int(q.get("start", 0) or 0)
        count = int(q.get("count", PAGE_SIZE) or PAGE_SIZE)
        if start + count < int(data.get("total") or 0) and data.get("data"):
            out.append((list_url(origin, start + count), "page"))
        return out, f"PeerTube: список видео с {start}"
    if isinstance(data, dict) and ("files" in data or "streamingPlaylists" in data):
        f = choose_file(data, max_height)
        if f:
            out.append((f.get("fileDownloadUrl") or f.get("fileUrl"), "media"))
        for key in ("thumbnailPath", "previewPath"):
            if data.get(key):
                out.append((origin + data[key], "asset"))
        return out, data.get("name")
    return out, None
