"""Командная строка: python -m azbyka_reserv <команда> [параметры]."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import __version__
from .config import load_config
from .fsutil import human_bytes

log = logging.getLogger("azbyka_reserv")


def setup_logging(data_dir: str, verbose: bool) -> None:
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("azbyka_reserv")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fh = logging.FileHandler(os.path.join(data_dir, "logs", time.strftime("crawl-%Y%m%d.log")), encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(ch)


def _config(args):
    path = args.config
    if path is None and os.path.exists("config.toml"):
        path = "config.toml"
    return load_config(path)


def cmd_crawl(args) -> int:
    from .crawler import Crawler

    cfg = _config(args)
    setup_logging(args.data, args.verbose)
    log.info("azbyka-reserv %s: данные в %s", __version__, os.path.abspath(args.data))
    c = Crawler(cfg, args.data)
    _keep_awake(True)
    try:
        c.run(max_seconds=args.max_minutes * 60 if args.max_minutes else 0)
    finally:
        _keep_awake(False)
    counts = c.store.counts()
    log.info("Итог: %s. Причина остановки: %s", counts, c.stop_reason or "—")
    queued = counts.get("queued", 0)
    # 0 — всё скачано; 3 — осталось в очереди (обёртка перезапустит);
    # 4 — мало места на диске; 130 — остановлено пользователем
    if c.stop_reason.startswith("мало места"):
        return 4
    if c.stop_reason.startswith("прервано"):
        return 130
    return 0 if queued == 0 and c.stop_reason.startswith("очередь пуста") else 3


def _keep_awake(on: bool) -> None:
    """Windows: не давать компьютеру уснуть, пока идёт сбор."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:  # noqa: BLE001
        pass


def cmd_status(args) -> int:
    from .report import print_status

    print_status(args.data, by=args.by, top=args.top)
    return 0


def cmd_retry(args) -> int:
    from .store import Store

    st = Store(os.path.join(args.data, "state.sqlite"))
    where = "status IN (%s)" % ",".join("?" * len(args.status))
    params: tuple = tuple(args.status)
    if args.match:
        where += " AND url LIKE ?"
        params += (f"%{args.match}%",)
    n = st.requeue_where(where, params)
    print(f"возвращено в очередь: {n}")
    return 0


def cmd_requeue_section(args) -> int:
    """Перекачать (обновить) уже скачанное: условные запросы, неизменённое не качается."""
    from .store import Store

    st = Store(os.path.join(args.data, "state.sqlite"))
    where = "status='done' AND kind IN ('page','sitemap')"
    params: tuple = ()
    if args.match:
        where += " AND url LIKE ?"
        params = (f"%{args.match}%",)
    if args.older_days:
        where += " AND fetched_at < ?"
        params += (time.time() - args.older_days * 86400,)
    n = st.requeue_where(where, params)
    print(f"поставлено на обновление: {n}")
    return 0


def cmd_probe(args) -> int:
    from .probe import run_probe

    cfg = _config(args)
    out = run_probe(cfg, args.out, limit=args.limit)
    print(f"Отчёт пробы: {out}")
    return 0


def cmd_serve(args) -> int:
    from .serve import serve

    serve(args.data, host=args.host, port=args.port, cfg=_config(args))
    return 0


def cmd_index(args) -> int:
    from .search import build_index

    setup_logging(args.data, args.verbose)
    build_index(args.data, rebuild=args.rebuild)
    return 0


def cmd_catalog(args) -> int:
    from .catalog import build_catalog

    out = build_catalog(args.data)
    print(f"Каталог файлов: {out}")
    return 0


def cmd_video(args) -> int:
    from .video import download_embeds

    setup_logging(args.data, args.verbose)
    return download_embeds(args.data, only_hosts=args.hosts, limit=args.limit, quality=args.quality)


def cmd_verify(args) -> int:
    from .report import verify_files

    return verify_files(args.data, fix=args.fix)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m azbyka_reserv",
        description="Офлайн-резерв портала «Азбука веры» (azbyka.ru).",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, config=True):
        sp.add_argument("--data", default="data", help="папка для архива (по умолчанию ./data)")
        if config:
            sp.add_argument("--config", default=None, help="свой config.toml (по умолчанию ./config.toml, если есть)")
        sp.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")

    sp = sub.add_parser("crawl", help="скачивать сайт (можно прерывать и запускать снова — продолжит)")
    common(sp)
    sp.add_argument("--max-minutes", type=float, default=0, help="остановиться через N минут (0 — до конца)")
    sp.set_defaults(func=cmd_crawl)

    sp = sub.add_parser("status", help="сколько скачано, по разделам, ошибки")
    common(sp, config=False)
    sp.add_argument("--by", choices=["section", "kind", "status", "ctype"], default="section")
    sp.add_argument("--top", type=int, default=40)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("retry", help="повторить неудачные (по умолчанию error и auth)")
    common(sp, config=False)
    sp.add_argument("--status", nargs="+", default=["error", "auth"])
    sp.add_argument("--match", default="", help="только URL, содержащие эту строку")
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("refresh", help="обновить уже скачанные страницы (условными запросами)")
    common(sp, config=False)
    sp.add_argument("--match", default="")
    sp.add_argument("--older-days", type=float, default=0)
    sp.set_defaults(func=cmd_requeue_section)

    sp = sub.add_parser("probe", help="разведка: robots, карты сайта, образцы страниц всех разделов")
    common(sp)
    sp.add_argument("--out", default="probe", help="папка для отчёта")
    sp.add_argument("--limit", type=int, default=3, help="образцов страниц на раздел")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("serve", help="офлайн-просмотр архива в браузере (http://localhost:8080)")
    common(sp)
    sp.add_argument("--host", default="127.0.0.1", help="0.0.0.0 — раздавать по локальной сети/Wi-Fi")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("index", help="построить полнотекстовый поиск по скачанному")
    common(sp, config=False)
    sp.add_argument("--rebuild", action="store_true")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("catalog", help="HTML-каталог книг/аудио/нот для просмотра без сервера")
    common(sp, config=False)
    sp.set_defaults(func=cmd_catalog)

    sp = sub.add_parser("video", help="скачать встроенные видео (YouTube/RuTube/VK) через yt-dlp")
    common(sp, config=False)
    sp.add_argument("--hosts", nargs="*", default=None, help="только эти хосты, напр. rutube.ru youtube.com")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--quality", default="best[height<=720]/best", help="формат yt-dlp")
    sp.set_defaults(func=cmd_video)

    sp = sub.add_parser("verify", help="проверить, что файлы архива на месте и не испорчены")
    common(sp, config=False)
    sp.add_argument("--fix", action="store_true", help="поставить пропавшие/битые файлы на перекачку")
    sp.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    return args.func(args)
