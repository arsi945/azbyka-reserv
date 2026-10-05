"""Статический HTML-каталог скачанных файлов (книги, ноты, аудио, видео).

Открывается двойным щелчком, без сервера и без интернета: ссылки ведут
прямо на файлы в папке ``mirror``. Плюс CSV для таблиц.
"""

from __future__ import annotations

import csv
import html
import os
import shutil
import sqlite3
import tempfile
import urllib.parse

from .fsutil import human_bytes
from .urls import url_ext

PER_PAGE = 1000
CHUNK = 2000

CATEGORIES = [
    ("books", "Книги (EPUB, FB2, PDF, DjVu…)", {"epub", "fb2", "fb2.zip", "mobi", "azw3", "djvu", "djv", "pdf", "doc", "docx", "rtf", "odt", "txt", "chm"}),
    ("notes", "Ноты для хора (PDF и др.)", None),
    ("audio", "Аудио (MP3, M4B…)", {"mp3", "m4b", "m4a", "ogg", "oga", "opus", "wav", "flac", "aac", "wma"}),
    ("video", "Видео", {"mp4", "m4v", "webm", "mkv", "avi", "mov", "flv", "3gp", "mpg", "mpeg", "wmv"}),
    ("archives", "Архивы", {"zip", "rar", "7z", "gz", "tgz"}),
]


def _path_ext(path: str) -> str:
    # путь файла — не URL: '#' и '?' в имени не начинают фрагмент/запрос
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".fb2.zip"):
        return "fb2.zip"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _category(url: str, path: str) -> str | None:
    ext = url_ext(url) or _path_ext(path)
    if "/kliros/" in url and ext in ("pdf", "mus", "mid", "midi", "nwc", "sib", "musx", "mxl", "zip"):
        return "notes"
    for key, _, exts in CATEGORIES:
        if exts and ext in exts:
            return key
    return None


def _quote_path(relpath: str) -> str:
    # каждый сегмент отдельно: кириллица -> %D0%9A…, а '#', '?', '%', "'" не ломают ссылку
    return "/".join(urllib.parse.quote(seg, safe="") for seg in relpath.replace("\\", "/").split("/"))


def _href(relpath: str) -> str:
    return "../mirror/" + _quote_path(relpath)


_FILES_SQL = """
  SELECT f.id, f.url, f.path, f.size, p.title AS ptitle
  FROM urls f LEFT JOIN urls p ON p.id = f.parent_id
  WHERE f.id > ? AND f.status='done' AND f.path IS NOT NULL AND f.kind IN ('media','page','asset')
    AND f.content_type NOT LIKE 'text/html%' AND f.content_type NOT LIKE 'image/%'
    AND f.content_type NOT LIKE 'text/css%' AND f.content_type NOT LIKE '%javascript%'
  ORDER BY f.id LIMIT ?
"""


def _chunks(conn: sqlite3.Connection, sql: str):
    """Выборка короткими порциями по id: не держит снимок базы, пока идёт обход."""
    last = 0
    while True:
        rows = conn.execute(sql, (last, CHUNK)).fetchall()
        if not rows:
            return
        last = rows[-1][0]
        yield from rows


def build_catalog(data_dir: str) -> str:
    conn = sqlite3.connect(os.path.join(data_dir, "state.sqlite"), timeout=60)
    conn.row_factory = sqlite3.Row
    out_dir = os.path.join(data_dir, "catalog")
    os.makedirs(out_dir, exist_ok=True)
    # на категорию — список компактных кортежей (название страницы, путь, размер, внешний?);
    # строки CSV сразу пишутся во временные файлы по категориям
    groups: dict[str, list[tuple[str, str, int, bool]]] = {k: [] for k, _, _ in CATEGORIES}
    parts = {k: tempfile.TemporaryFile("w+", encoding="utf-8", newline="") for k, _, _ in CATEGORIES}
    try:
        writers = {k: csv.writer(fh, delimiter=";") for k, fh in parts.items()}
        for r in _chunks(conn, _FILES_SQL):
            cat = _category(r["url"], r["path"])
            if cat:
                ptitle = r["ptitle"] or ""
                groups[cat].append((ptitle, r["path"], r["size"] or 0, False))
                writers[cat].writerow([cat, ptitle, r["path"].rsplit("/", 1)[-1], r["size"] or "", r["url"], r["path"]])
        # видео, скачанные командой video
        try:
            for r in _chunks(conn, "SELECT rowid, url, page_url, path FROM embeds WHERE rowid > ? AND status='done'"
                                   " AND path IS NOT NULL ORDER BY rowid LIMIT ?"):
                path = r["path"].replace("\\", "/")
                groups["video"].append(("", path, 0, True))
                writers["video"].writerow(["video", "", path.rsplit("/", 1)[-1], "", r["url"], path])
        except sqlite3.OperationalError:
            pass  # старая база без таблицы embeds

        with open(os.path.join(out_dir, "files.csv"), "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerow(["категория", "страница", "файл", "размер", "адрес", "путь"])
            for key, _, _ in CATEGORIES:
                parts[key].seek(0)
                shutil.copyfileobj(parts[key], fh)
    finally:
        conn.close()
        for fh in parts.values():
            fh.close()

    index_items = []
    for key, title, _ in CATEGORIES:
        items = groups.pop(key)
        items.sort(key=lambda it: ((it[0] or "~").lower(), it[1].lower()))
        total_size = sum(it[2] for it in items)
        pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        for pi in range(pages):
            rows = []
            last_parent = None
            for parent, path, size, external in items[pi * PER_PAGE : (pi + 1) * PER_PAGE]:
                if parent != last_parent:
                    rows.append(f"<tr><th colspan=2>{html.escape(parent or 'Без страницы')}</th></tr>")
                    last_parent = parent
                name = path.rsplit("/", 1)[-1]
                href = ("../" + _quote_path(path)) if external else _href(path)
                shown = human_bytes(size) if size else ""
                rows.append(f"<tr><td><a href='{href}'>{html.escape(name)}</a></td><td class=s>{shown}</td></tr>")
            nav = " ".join(
                (f"<b>{i + 1}</b>" if i == pi else f"<a href='{key}-{i + 1}.html'>{i + 1}</a>") for i in range(pages)
            )
            doc = _doc(f"{title} — стр. {pi + 1}", f"<p><a href='index.html'>← Каталог</a></p><h1>{html.escape(title)}</h1>"
                       f"<p class=s>Файлов: {len(items)}, объём {human_bytes(total_size)}</p><p>{nav}</p>"
                       f"<table>{''.join(rows)}</table><p>{nav}</p>")
            with open(os.path.join(out_dir, f"{key}-{pi + 1}.html"), "w", encoding="utf-8") as fh:
                fh.write(doc)
        index_items.append(f"<li><a href='{key}-1.html'>{html.escape(title)}</a> — {len(items)} файлов, {human_bytes(total_size)}</li>")
        del items
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
