"""Статический HTML-каталог скачанных файлов (книги, ноты, аудио, видео).

Открывается двойным щелчком, без сервера и без интернета: ссылки ведут
прямо на файлы в папке ``mirror``. Плюс CSV для таблиц.
"""

from __future__ import annotations

import csv
import html
import os
import sqlite3
import urllib.parse

from .fsutil import human_bytes
from .urls import url_ext

PER_PAGE = 1000

CATEGORIES = [
    ("books", "Книги (EPUB, FB2, PDF, DjVu…)", {"epub", "fb2", "fb2.zip", "mobi", "azw3", "djvu", "djv", "pdf", "doc", "docx", "rtf", "odt", "txt", "chm"}),
    ("notes", "Ноты для хора (PDF и др.)", None),
    ("audio", "Аудио (MP3, M4B…)", {"mp3", "m4b", "m4a", "ogg", "oga", "opus", "wav", "flac", "aac", "wma"}),
    ("video", "Видео", {"mp4", "m4v", "webm", "mkv", "avi", "mov", "flv", "3gp", "mpg", "mpeg", "wmv"}),
    ("archives", "Архивы", {"zip", "rar", "7z", "gz", "tgz"}),
]


def _category(url: str, path: str) -> str | None:
    ext = url_ext(url) or url_ext("x/" + path.rsplit("/", 1)[-1])
    if "/kliros/" in url and ext in ("pdf", "mus", "mid", "midi", "nwc", "sib", "musx", "mxl", "zip"):
        return "notes"
    for key, _, exts in CATEGORIES:
        if exts and ext in exts:
            return key
    return None


def _href(relpath: str) -> str:
    return "../mirror/" + "/".join(urllib.parse.quote(seg) for seg in relpath.split("/"))


def build_catalog(data_dir: str) -> str:
    conn = sqlite3.connect(os.path.join(data_dir, "state.sqlite"), timeout=60)
    conn.row_factory = sqlite3.Row
    out_dir = os.path.join(data_dir, "catalog")
    os.makedirs(out_dir, exist_ok=True)
    groups: dict[str, list[dict]] = {k: [] for k, _, _ in CATEGORIES}
    q = """
      SELECT f.url, f.path, f.size, f.content_type, p.title AS ptitle, p.url AS purl
      FROM urls f LEFT JOIN urls p ON p.id = f.parent_id
      WHERE f.status='done' AND f.path IS NOT NULL AND f.kind IN ('media','page','asset')
        AND f.content_type NOT LIKE 'text/html%' AND f.content_type NOT LIKE 'image/%'
        AND f.content_type NOT LIKE 'text/css%' AND f.content_type NOT LIKE '%javascript%'
    """
    for r in conn.execute(q):
        cat = _category(r["url"], r["path"])
        if cat:
            groups[cat].append(dict(r))
    # видео, скачанные командой video
    for r in conn.execute("SELECT url, page_url, path FROM embeds WHERE status='done' AND path IS NOT NULL"):
        groups["video"].append({"url": r["url"], "path": r["path"], "size": None, "ptitle": None, "purl": r["page_url"], "external": True})

    with open(os.path.join(out_dir, "files.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["категория", "страница", "файл", "размер", "адрес", "путь"])
        for key, items in groups.items():
            for it in items:
                w.writerow([key, it.get("ptitle") or "", it["path"].rsplit("/", 1)[-1], it.get("size") or "", it["url"], it["path"]])

    index_items = []
    for key, title, _ in CATEGORIES:
        items = sorted(groups[key], key=lambda it: ((it.get("ptitle") or "~").lower(), it["path"].lower()))
        total_size = sum(it.get("size") or 0 for it in items)
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        for pi in range(pages):
            chunk = items[pi * PER_PAGE : (pi + 1) * PER_PAGE]
            rows = []
            last_parent = None
            for it in chunk:
                parent = it.get("ptitle") or ""
                if parent != last_parent:
                    rows.append(f"<tr><th colspan=2>{html.escape(parent or 'Без страницы')}</th></tr>")
                    last_parent = parent
                name = it["path"].rsplit("/", 1)[-1]
                href = ("../" + "/".join(urllib.parse.quote(s) for s in it["path"].split("/"))) if it.get("external") else _href(it["path"])
                size = human_bytes(it["size"]) if it.get("size") else ""
                rows.append(f"<tr><td><a href='{href}'>{html.escape(name)}</a></td><td class=s>{size}</td></tr>")
            nav = " ".join(
                (f"<b>{i + 1}</b>" if i == pi else f"<a href='{key}-{i + 1}.html'>{i + 1}</a>") for i in range(pages)
            )
            doc = _doc(f"{title} — стр. {pi + 1}", f"<p><a href='index.html'>← Каталог</a></p><h1>{html.escape(title)}</h1>"
                       f"<p class=s>Файлов: {len(items)}, объём {human_bytes(total_size)}</p><p>{nav}</p>"
                       f"<table>{''.join(rows)}</table><p>{nav}</p>")
            with open(os.path.join(out_dir, f"{key}-{pi + 1}.html"), "w", encoding="utf-8") as fh:
                fh.write(doc)
        index_items.append(f"<li><a href='{key}-1.html'>{html.escape(title)}</a> — {len(items)} файлов, {human_bytes(total_size)}</li>")
    doc = _doc("Каталог файлов архива", "<h1>Каталог файлов офлайн-архива «Азбука веры»</h1>"
               f"<ul>{''.join(index_items)}</ul><p class=s>Список всех файлов для Excel: files.csv. "
               "Сам сайт со ссылками и поиском открывается командой serve (prosmotr.bat).</p>")
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


def _doc(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:1000px;margin:1.5em auto;padding:0 16px;color:#222;background:#fff}}
a{{color:#7a2a00}} th{{text-align:left;padding-top:1em;border-bottom:1px solid #ddd}} td{{padding:1px 8px}} .s{{color:#666;white-space:nowrap}}
table{{border-collapse:collapse;width:100%}}</style></head><body>{body}</body></html>"""
