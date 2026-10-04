"""Статические проверки скриптов запуска (zapusk.bat, prosmotr.bat, servis.bat, zapusk.sh)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = ["zapusk.bat", "prosmotr.bat", "servis.bat"]
TRAILING_BACKSLASH_FIX = 'if "%DATA:~-1%"=="\\" set "DATA=%DATA%."'


def _bytes(name: str) -> bytes:
    return (ROOT / name).read_bytes()


def _text(name: str) -> str:
    return _bytes(name).decode("utf-8")


def _lines(name: str) -> list[str]:
    return _text(name).split("\r\n")


def _find_python_block(name: str) -> str:
    text = _text(name)
    start = text.index("\r\n:find_python\r\n")
    return text[start:]


@pytest.mark.parametrize("name", BATS)
def test_bat_encoding_and_line_endings(name):
    data = _bytes(name)
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM ломает первую строку в cmd"
    data.decode("utf-8")  # корректный UTF-8
    assert data.count(b"\n") == data.count(b"\r\n"), "все строки должны оканчиваться CRLF"
    assert data.endswith(b"\r\n")


@pytest.mark.parametrize("name", BATS)
def test_bat_basics(name):
    lines = _lines(name)
    text = _text(name)
    assert lines[0] == "@echo off"
    assert "chcp 65001 >nul" in lines[:10]
    code = "\n".join(ln for ln in lines if not ln.strip().lower().startswith("rem")).lower()
    assert 'pushd "%~dp0"' in text
    assert "cd /d" not in code, "cd /d не работает с сетевыми папками (UNC) — нужен pushd"
    assert "popd" in code
    assert "timeout /t" not in code, "timeout не работает без консоли — нужен ping"
    assert TRAILING_BACKSLASH_FIX in lines
    # исправление идёт после всех присваиваний DATA из параметров
    fix_at = lines.index(TRAILING_BACKSLASH_FIX)
    sets = [i for i, ln in enumerate(lines) if re.search(r'set "DATA=%(~\w|ARG2%)', ln)]
    assert sets and max(sets) < fix_at


@pytest.mark.parametrize("name", BATS)
def test_bat_find_python(name):
    text = _text(name)
    assert "call :find_python" in text
    block = _find_python_block(name)
    assert '"%PYRC%"=="9009"' in block, "заглушка Microsoft Store (код 9009) = Python не найден"
    assert "py -3" in block
    assert "sys.version_info >= (3, 11)" in block
    assert "https://www.python.org/downloads/" in block


def test_find_python_is_identical_everywhere():
    blocks = {name: _find_python_block(name) for name in BATS}
    assert len(set(blocks.values())) == 1, "подпрограмма :find_python должна совпадать во всех .bat"


@pytest.mark.parametrize("name", BATS)
def test_bat_labels_resolve(name):
    lines = [ln.strip() for ln in _lines(name)]
    labels = {ln[1:].split()[0].lower() for ln in lines if ln.startswith(":") and not ln.startswith("::")}
    targets = set()
    for ln in lines:
        if ln.lower().startswith("rem"):
            continue
        targets.update(m.lower() for m in re.findall(r"\bgoto\s+:?([\w-]+)", ln, re.I))
        targets.update(m.lower() for m in re.findall(r"\bcall\s+:([\w-]+)", ln, re.I))
    targets.discard("eof")
    assert targets, "нет переходов?"
    assert targets <= labels, f"нет меток: {sorted(targets - labels)}"


@pytest.mark.parametrize("name", BATS)
def test_bat_rem_lines_have_no_percent(name):
    # в rem-строках cmd всё равно раскрывает %, ошибка вида %~ обрывает скрипт
    for ln in _lines(name):
        if ln.strip().lower().startswith("rem "):
            assert "%" not in ln, ln


def test_zapusk_bat_exit_codes_and_delay():
    text = _text("zapusk.bat")
    for code in ("0", "3", "4", "5", "6", "130"):
        assert f'if "%RC%"=="{code}" goto' in text, code
    assert "ping -n 61 127.0.0.1 >nul" in text
    assert "truststore certifi" in text
    assert "py -3 -m pip install truststore certifi" in text
    assert "QUICKFAILS" in text and "LSS 20" in text
    assert "goto startup_error" in text
    # ошибка запуска и «уже запущено» — пауза, без цикла
    for label in (":startup_error", ":busy", ":tls", ":diskfull"):
        section = text.split("\r\n" + label + "\r\n", 1)[1].split("\r\n:", 1)[0]
        assert "pause" in section and "goto loop" not in section, label


def test_zapusk_bat_onedrive_warning():
    text = _text("zapusk.bat")
    for var in ("%OneDrive%", "%OneDriveConsumer%", "%OneDriveCommercial%"):
        assert var in text
    assert "zapusk.bat D:\\azbyka" in text
    assert "ping -n 11 127.0.0.1 >nul" in text


def test_prosmotr_bat():
    text = _text("prosmotr.bat")
    assert 'start "" http://localhost:8080/__azr__/' in text
    assert "serve --data" in text
    assert "pause" in text.split("serve --data", 1)[1]


def test_servis_bat_commands():
    text = _text("servis.bat")
    for cmd in ("status", "index", "catalog", "video", "verify", "retry", "relink", "refresh"):
        assert re.search(rf"for %%c in \([^)]*\b{cmd}\b", text), cmd
    assert "shift /2" in text, "доп. параметры после папки передаются программе"
    assert '--data "%DATA%"%EXTRA%' in text
    assert 'start "" "%DATA%\\catalog\\index.html"' in text


# ---------- zapusk.sh ----------


def test_zapusk_sh_static():
    data = _bytes("zapusk.sh")
    assert b"\r" not in data
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert os.stat(ROOT / "zapusk.sh").st_mode & stat.S_IXUSR
    text = data.decode("utf-8")
    assert "sleep 60" in text and "date +%s" in text
    for code in ("0", "3", "4", "5", "6", "130"):
        assert re.search(rf"^\s+{code}\)", text, re.M), code


bash = shutil.which("bash")


@pytest.mark.skipif(bash is None, reason="нет bash")
def test_zapusk_sh_syntax():
    subprocess.run([bash, "-n", str(ROOT / "zapusk.sh")], check=True)


FAKE_PY = """#!/usr/bin/env bash
case "$1" in -c) exit 0;; -V) echo "Python 3.99"; exit 0;; esac
if [ "$3" = crawl ]; then
  codes=$(cat "$CODES_FILE"); first=${codes%% *}; rest=${codes#* }
  [ "$rest" = "$codes" ] && rest=""
  echo "$rest" > "$CODES_FILE"; echo "CRAWL $first"; exit "$first"
fi
echo "CMD $3"; exit 0
"""


@pytest.mark.skipif(bash is None, reason="нет bash")
@pytest.mark.parametrize(
    "codes, exit_code, crawls, sleeps",
    [
        ("0", 0, 1, []),
        ("3 3 0", 0, 3, ["10", "10"]),
        ("4", 4, 1, []),
        ("5", 5, 1, []),
        ("6", 6, 1, []),
        ("130", 130, 1, []),
        ("1 1", 1, 2, ["60"]),  # два быстрых сбоя подряд — ошибка запуска, без цикла
        ("1 3 1 0", 0, 4, ["60", "10", "60"]),  # код 3 сбрасывает счётчик быстрых сбоев
    ],
)
def test_zapusk_sh_exit_codes(tmp_path, codes, exit_code, crawls, sleeps):
    fake = tmp_path / "python"
    fake.write_text(FAKE_PY)
    fake.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "sleep"
    shim.write_text('#!/bin/sh\necho "SLEEP $1"\n')
    shim.chmod(0o755)
    codes_file = tmp_path / "codes"
    codes_file.write_text(codes)
    env = dict(os.environ, PYTHON=str(fake), CODES_FILE=str(codes_file), PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(
        [bash, str(ROOT / "zapusk.sh"), str(tmp_path / "data")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == exit_code, r.stdout + r.stderr
    assert r.stdout.count("CRAWL ") == crawls
    assert re.findall(r"^SLEEP (\d+)$", r.stdout, re.M) == sleeps
    if exit_code == 0:
        assert "CMD status" in r.stdout
