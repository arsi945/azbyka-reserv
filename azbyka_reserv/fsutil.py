"""Работа с файлами зеркала: длинные пути Windows, атомарная запись."""

from __future__ import annotations

import os

IS_WINDOWS = os.name == "nt"


def fs_path(path: str) -> str:
    r"""Абсолютный путь; на Windows — с префиксом ``\\?\`` (снимает лимит 260 символов)."""
    p = os.path.abspath(path)
    if IS_WINDOWS and not p.startswith("\\\\?\\"):
        if p.startswith("\\\\"):
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


def mirror_file(mirror_root: str, relpath: str) -> str:
    return fs_path(os.path.join(mirror_root, *relpath.split("/")))


def write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def acquire_lock(data_dir: str):
    """Исключительная блокировка папки архива (один сбор на папку).

    Возвращает открытый файл-замок (держать до конца работы) или None, если
    папку уже использует другой процесс. Блокировку снимает ОС при выходе.
    """
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, ".lock")
    f = open(path, "a+")
    try:
        if IS_WINDOWS:
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    _HELD_LOCKS.append(f)
    return f


_HELD_LOCKS: list = []
