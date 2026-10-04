"""Скачивание встроенных видео/аудио (YouTube, RuTube, VK…) через yt-dlp.

yt-dlp ставится отдельно: ``pip install yt-dlp`` или файл yt-dlp.exe
рядом со скриптами (https://github.com/yt-dlp/yt-dlp/releases).
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys

log = logging.getLogger("azbyka_reserv")

_YT_EMBED = re.compile(r"(?:youtube(?:-nocookie)?\.com/embed/|youtu\.be/)([\w-]{6,})", re.I)


def _yt_dlp_cmd() -> list[str] | None:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if exe:
        return [exe]
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    for cand in ("yt-dlp.exe", "yt-dlp"):
        p = os.path.join(here, cand)
        if os.path.exists(p):
            return [p]
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def canonical_video_url(url: str) -> str:
    m = _YT_EMBED.search(url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url.split("#", 1)[0]


def download_embeds(data_dir: str, only_hosts: list[str] | None = None, limit: int = 0, quality: str = "best") -> int:
    cmd = _yt_dlp_cmd()
    if cmd is None:
        print("Не найден yt-dlp. Установите: pip install yt-dlp  (или положите yt-dlp.exe рядом).")
        return 2
    conn = sqlite3.connect(os.path.join(data_dir, "state.sqlite"), timeout=60)
    conn.row_factory = sqlite3.Row
    out_root = os.path.join(data_dir, "video")
    os.makedirs(out_root, exist_ok=True)
    archive = os.path.join(out_root, "downloaded.txt")
    rows = conn.execute("SELECT url, page_url FROM embeds WHERE status IN ('new','error') ORDER BY rowid").fetchall()
    done = 0
    for r in rows:
        url = r["url"]
        if only_hosts and not any(h in url for h in only_hosts):
            continue
        if limit and done >= limit:
            break
        target = canonical_video_url(url)
        key = re.sub(r"[^\w.-]+", "_", target.split("://", 1)[-1])[:120]
        out_dir = os.path.join(out_root, key)
        os.makedirs(out_dir, exist_ok=True)
        args = cmd + [
            "-f", quality, "--no-overwrites", "--continue", "--no-playlist",
            "--download-archive", archive, "--write-info-json", "--write-thumbnail",
            "--retries", "10", "--fragment-retries", "10", "--sleep-requests", "1",
            "-o", os.path.join(out_dir, "%(title).120B [%(id)s].%(ext)s"), target,
        ]
        log.info("видео: %s", target)
        proc = subprocess.run(args)
        media = [p for p in glob.glob(os.path.join(out_dir, "*")) if not p.endswith((".json", ".part", ".jpg", ".webp", ".png", ".ytdl"))]
        if proc.returncode == 0 and media:
            rel = os.path.relpath(media[0], data_dir).replace(os.sep, "/")
            conn.execute("UPDATE embeds SET status='done', path=?, error=NULL WHERE url=?", (rel, url))
            done += 1
        else:
            conn.execute("UPDATE embeds SET status='error', error=? WHERE url=?", (f"yt-dlp код {proc.returncode}", url))
        conn.commit()
    print(f"Скачано видео: {done}")
    return 0
