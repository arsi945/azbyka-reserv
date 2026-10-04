@echo off
rem Офлайн-просмотр архива в браузере: http://localhost:8080/
rem prosmotr.bat [папка_архива] [set]  — "set" раздаёт архив по локальной сети/Wi-Fi
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
set "DATA=%~dp0data"
if not "%~1"=="" set "DATA=%~1"
set "HOSTARG=--host 127.0.0.1"
if /i "%~2"=="set" set "HOSTARG=--host 0.0.0.0"
start "" http://localhost:8080/__azr__/
%PY% -m azbyka_reserv serve --data "%DATA%" %HOSTARG% --port 8080
pause
