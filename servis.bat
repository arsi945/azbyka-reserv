@echo off
rem Служебные команды: состояние, поиск, каталог, видео, проверка.
rem servis.bat status | index | catalog | video | verify | retry   [папка_архива]
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
set "CMD=%~1"
if "%CMD%"=="" set "CMD=status"
set "DATA=%~dp0data"
if not "%~2"=="" set "DATA=%~2"
%PY% -m azbyka_reserv %CMD% --data "%DATA%"
if /i "%CMD%"=="catalog" start "" "%DATA%\catalog\index.html"
pause
