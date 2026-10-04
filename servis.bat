@echo off
rem Служебные команды: состояние, поиск, каталог, видео, проверка, повтор, переразбор, обновление.
rem servis.bat [команда] [папка_архива] [доп. параметры]   (по умолчанию: status для .\data)
rem команды: status index catalog video verify retry relink refresh
rem пример:  servis.bat verify E:\azbyka --fix
chcp 65001 >nul
setlocal
rem pushd, а не cd /d: так работает и запуск из сетевой папки.
pushd "%~dp0" || goto nodir
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "RC=1"
set "CMD=%~1"
if "%CMD%"=="" set "CMD=status"
set "SUB="
for %%c in (status index catalog video verify retry relink refresh) do if /i "%CMD%"=="%%c" set "SUB=%%c"
if not defined SUB goto usage
call :find_python
if errorlevel 1 goto quit

rem Второй параметр — папка архива; если он начинается с "-", это уже доп. параметр.
rem Всё после папки передаётся программе как есть (например --fix).
set "DATA=%~dp0data"
set "EXTRA="
set "ARG2=%~2"
if not defined ARG2 goto args_done
if "%ARG2:~0,1%"=="-" goto collect
set "DATA=%ARG2%"
shift /2
:collect
if "%~2"=="" goto args_done
set EXTRA=%EXTRA% %2
shift /2
goto collect
:args_done
rem Путь с обратной косой чертой на конце (E:\) Windows передал бы как E: с кавычкой,
rem поэтому дописываем точку: E:\. — тот же корень диска.
if "%DATA:~-1%"=="\" set "DATA=%DATA%."
if exist "%DATA%\" goto data_ok
echo Папка архива не найдена: "%DATA%"
echo Укажите её явно, например:  servis.bat %SUB% E:\azbyka
pause
goto quit
:data_ok
echo Команда: %SUB%   Папка архива: "%DATA%"
%PY% -m azbyka_reserv %SUB% --data "%DATA%"%EXTRA%
set "RC=%errorlevel%"
if not "%RC%"=="0" goto failed
if not "%SUB%"=="catalog" goto done
if exist "%DATA%\catalog\index.html" start "" "%DATA%\catalog\index.html"
goto done
:failed
echo.
echo Команда завершилась с ошибкой (код %RC%). Прочитайте сообщение выше.
:done
pause
goto quit

:usage
if /i "%CMD%"=="help" goto usage_text
echo Неизвестная команда: "%CMD%"
echo.
:usage_text
echo Использование: servis.bat [команда] [папка_архива] [доп. параметры]
echo   status   — сколько скачано, по разделам, ошибки (по умолчанию)
echo   index    — построить полнотекстовый поиск по скачанному
echo   catalog  — HTML-каталог книг, аудио и нот; откроется в браузере
echo   video    — скачать встроенные видео через yt-dlp
echo   verify   — проверить, что файлы архива на месте; с --fix поставить битые на перекачку
echo   retry    — повторить неудачные загрузки
echo   relink   — заново разобрать скачанное по новым правилам, без сети
echo   refresh  — обновить уже скачанные страницы
echo Пример:  servis.bat verify E:\azbyka --fix
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
