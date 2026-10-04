@echo off
rem Офлайн-просмотр архива в браузере: http://localhost:8080/
rem prosmotr.bat [папка_архива] [set]  — "set" раздаёт архив по локальной сети/Wi-Fi
rem prosmotr.bat set                   — то же для папки data рядом с этим файлом
chcp 65001 >nul
setlocal
rem pushd, а не cd /d: так работает и запуск из сетевой папки.
pushd "%~dp0" || goto nodir
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "RC=1"
call :find_python
if errorlevel 1 goto quit
set "DATA=%~dp0data"
set "HOSTARG=--host 127.0.0.1"
if /i "%~1"=="set" goto lan_default
if not "%~1"=="" set "DATA=%~1"
if /i "%~2"=="set" set "HOSTARG=--host 0.0.0.0"
goto args_done
:lan_default
set "HOSTARG=--host 0.0.0.0"
:args_done
rem Путь с обратной косой чертой на конце (E:\) Windows передал бы как E: с кавычкой,
rem поэтому дописываем точку: E:\. — тот же корень диска.
if "%DATA:~-1%"=="\" set "DATA=%DATA%."
if exist "%DATA%\" goto data_ok
echo Папка архива не найдена: "%DATA%"
echo Укажите её явно, например:  prosmotr.bat E:\azbyka
pause
goto quit
:data_ok
echo Архив: "%DATA%"
echo Если страница в браузере не открылась сразу, обновите её через пару секунд.
echo Чтобы остановить просмотр, закройте это окно.
start "" http://localhost:8080/__azr__/
%PY% -m azbyka_reserv serve --data "%DATA%" %HOSTARG% --port 8080
set "RC=%errorlevel%"
if "%RC%"=="0" goto served
if "%RC%"=="130" goto served
if "%RC%"=="-1073741510" goto served
echo.
echo Просмотр остановлен с ошибкой (код %RC%). Прочитайте сообщение выше.
echo Если порт 8080 занят, возможно, просмотр уже открыт в другом окне.
:served
pause
goto quit

:quit
popd
exit /b %RC%

:nodir
echo Не удалось открыть папку программы: "%~dp0"
pause
exit /b 1

:find_python
rem Ищем Python 3.11+: сначала py -3 (стандартный запускатель), затем python.
rem Заглушка Microsoft Store (python.exe в WindowsApps) возвращает код 9009 —
rem это значит, что Python не установлен. Код 7 от проверки — слишком старая версия.
set "PY="
set "PY_OLD="
set "PYCHECK=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 7)"
where py >nul 2>nul
if errorlevel 1 goto fp_python
py -3 -c "%PYCHECK%" >nul 2>nul
set "PYRC=%errorlevel%"
if "%PYRC%"=="0" set "PY=py -3"
if defined PY exit /b 0
if "%PYRC%"=="7" set "PY_OLD=1"
:fp_python
where python >nul 2>nul
if errorlevel 1 goto fp_fail
python -c "%PYCHECK%" >nul 2>nul
set "PYRC=%errorlevel%"
if "%PYRC%"=="9009" goto fp_fail
if "%PYRC%"=="0" set "PY=python"
if defined PY exit /b 0
if "%PYRC%"=="7" set "PY_OLD=1"
:fp_fail
if defined PY_OLD goto fp_old
echo Не найден Python. Установите Python 3.11 или новее с https://www.python.org/downloads/
echo (при установке отметьте галочку "Add python.exe to PATH") и запустите этот файл снова.
echo Если Python уже установлен, а сообщение повторяется, отключите псевдонимы python.exe:
echo Параметры Windows — Приложения — Дополнительные параметры — Псевдонимы выполнения приложений.
pause
exit /b 1
:fp_old
echo Установлен слишком старый Python. Нужен 3.11 или новее: https://www.python.org/downloads/
pause
exit /b 1
