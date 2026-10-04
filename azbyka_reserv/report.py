"""Отчёты о ходе обхода и проверка целостности архива."""

from __future__ import annotations

import os
import sqlite3

from .fsutil import human_bytes, mirror_file

_HOST_SQL = "substr(substr(url, instr(url, '//') + 2), 1, instr(substr(url, instr(url, '//') + 2) || '/', '/') - 1)"
_SECTION_SQL = f"""
CASE WHEN url LIKE 'https://azbyka.ru/%' THEN
  '/' || substr(replace(substr(url, 19), '?', '/'), 1, instr(replace(substr(url, 19), '?', '/') || '/', '/') - 1)
WHEN {_HOST_SQL} LIKE '%.azbyka.ru' THEN {_HOST_SQL}
ELSE
  'внешн: ' || {_HOST_SQL}
END
"""

_BY = {
    "section": _SECTION_SQL,
    "kind": "kind",
    "status": "status",
    "ctype": "lower(trim(substr(content_type, 1, CASE WHEN instr(content_type, ';') > 0 THEN instr(content_type, ';') - 1 ELSE length(content_type) END)))",
}

_STATUS_RU = {
    "done": "скачано",
    "queued": "в очереди",
    "active": "в работе",
    "redirect": "редирект",
    "notfound": "нет (404)",
    "error": "ошибка",
    "skipped": "пропущено",
    "auth": "нужен вход",
}


def _open(data_dir: str) -> sqlite3.Connection:
    path = os.path.join(data_dir, "state.sqlite")
    if not os.path.exists(path):
        raise SystemExit(f"Нет базы {path}: сначала запустите crawl")
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def print_status(data_dir: str, by: str = "section", top: int = 40) -> None:
    conn = _open(data_dir)
    rows = conn.execute("SELECT status, COUNT(*) n, COALESCE(SUM(size),0) b FROM urls GROUP BY status").fetchall()
    total = sum(r["n"] for r in rows)
    print(f"Всего адресов: {total}")
    for r in sorted(rows, key=lambda r: -r["n"]):
        extra = f", {human_bytes(r['b'])}" if r["b"] else ""
        print(f"  {_STATUS_RU.get(r['status'], r['status']):<12} {r['n']:>10}{extra}")

    expr = _BY[by]
    print(f"\nПо группам ({by}), первые {top} по объёму:")
    q = f"""
      SELECT {expr} AS g,
             SUM(status='done') AS done, SUM(status IN ('queued','active')) AS queued,
             SUM(status IN ('error','auth')) AS bad, COALESCE(SUM(CASE WHEN status='done' THEN size END),0) AS bytes
      FROM urls GROUP BY g ORDER BY bytes DESC, done DESC LIMIT ?"""
    print(f"  {'группа':<40} {'скачано':>9} {'очередь':>9} {'ошибки':>7} {'объём':>10}")
    for r in conn.execute(q, (top,)):
        print(f"  {str(r['g'])[:40]:<40} {r['done']:>9} {r['queued']:>9} {r['bad']:>7} {human_bytes(r['bytes']):>10}")

    sk = conn.execute(
        "SELECT reason, SUM(n) n FROM skipped_stats GROUP BY reason ORDER BY n DESC"
    ).fetchall()
    if sk:
        print("\nНе обходились (по правилам):")
        names = {"exclude": "исключено конфигом", "robots": "запрет robots.txt", "date": "дата вне диапазона",
                 "query_cap": "лимит вариантов query", "depth": "глубина",
                 "login": "нужен вход (cookies_file)", "peertube": "служебные адреса видео"}
        for r in sk:
            print(f"  {names.get(r['reason'], r['reason']):<28} {r['n']:>10}")
        print("  самые частые:")
        for r in conn.execute("SELECT reason, prefix, n, example FROM skipped_stats ORDER BY n DESC LIMIT 12"):
            print(f"    [{r['reason']}] /{r['prefix']}: {r['n']}  напр. {r['example'][:110]}")

    errs = conn.execute(
        "SELECT status, http_status, error, COUNT(*) n, MIN(url) ex FROM urls WHERE status IN ('error','auth')"
        " GROUP BY status, http_status, error ORDER BY n DESC LIMIT 10"
    ).fetchall()
    if errs:
        print("\nОшибки (чаще всего):")
        for r in errs:
            print(f"  {r['n']:>7}  {_STATUS_RU.get(r['status'])} {r['http_status'] or ''} {(r['error'] or '')[:60]}  напр. {r['ex'][:90]}")
    emb = conn.execute("SELECT status, COUNT(*) n FROM embeds GROUP BY status").fetchall()
    if emb:
        print("\nВстроенные видео/плееры (команда video): " + ", ".join(f"{r['status']}={r['n']}" for r in emb))


def verify_files(data_dir: str, fix: bool = False) -> int:
    from .store import Store

    st = Store(os.path.join(data_dir, "state.sqlite"))
    mirror = os.path.join(data_dir, "mirror")
    missing = bad_size = checked = 0
    to_requeue: list[int] = []
    # порциями: длинный курсор не дал бы журналу SQLite сбрасываться
    for r in st.iter_chunks("id, path, size", "status='done' AND path IS NOT NULL"):
        checked += 1
        try:
            size = os.stat(mirror_file(mirror, r["path"])).st_size
        except OSError:
            missing += 1
            to_requeue.append(r["id"])
            continue
        if r["size"] is not None and size != r["size"]:
            bad_size += 1
            to_requeue.append(r["id"])
        if checked % 100000 == 0:
            print(f"  проверено {checked}…")
    print(f"Проверено файлов: {checked}; нет на диске: {missing}; размер не совпал: {bad_size}")
    if fix and to_requeue:
        with st.tx() as c:
            for i in range(0, len(to_requeue), 500):
                chunk = to_requeue[i : i + 500]
                c.execute(
                    "UPDATE urls SET status='queued', tries=0, next_try_at=0, etag=NULL, last_modified=NULL"
                    " WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk,
                )
        print(f"Поставлено на перекачку: {len(to_requeue)}. Запустите сбор (zapusk.bat).")
    st.close()
    return 0 if not to_requeue else 1
