@echo off
rem Сбор архива azbyka.ru. Можно закрывать окно и запускать снова — продолжит с места остановки.
rem Использование: zapusk.bat [папка_для_архива]   (по умолчанию .\data рядом с этим файлом)
chcp 65001 >nul
setlocal
rem Коды выхода crawl: 0 — всё скачано; 3 — работа осталась, перезапуск; 4 — мало места;
rem 5 — не проходит проверка сертификата TLS; 6 — сбор уже запущен для этой папки;
rem 130 — остановлено пользователем; прочие (1 — сбой) — перезапуск через минуту,
rem но два быстрых сбоя подряд считаем ошибкой запуска и останавливаемся.
rem pushd, а не cd /d: так работает и запуск из сетевой папки.
pushd "%~dp0" || goto nodir
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "RC=1"
call :find_python
if errorlevel 1 goto quit
set "DATA=%~dp0data"
if not "%~1"=="" set "DATA=%~1"
rem Путь с обратной косой чертой на конце (E:\) Windows передал бы как E: с кавычкой,
rem поэтому дописываем точку: E:\. — тот же корень диска.
if "%DATA:~-1%"=="\" set "DATA=%DATA%."
for %%I in ("%DATA%") do set "DATA_ABS=%%~fI"
echo Архив будет сохраняться в: "%DATA_ABS%"

set "IN_ONEDRIVE="
call :check_onedrive "%OneDrive%"
call :check_onedrive "%OneDriveConsumer%"
call :check_onedrive "%OneDriveCommercial%"
if not defined IN_ONEDRIVE goto start_crawl
echo.
echo ВНИМАНИЕ: папка архива находится внутри OneDrive: "%IN_ONEDRIVE%"
echo OneDrive будет выгружать в облако сотни тысяч файлов архива — это замедлит сбор
echo и переполнит облачное хранилище, а «Файлы по запросу» могут оставить на диске
echo только ссылки на облако, и без интернета архив не откроется.
echo Лучше хранить архив на отдельном диске или в папке вне OneDrive, например:
echo     zapusk.bat D:\azbyka
echo Сбор продолжится через 10 секунд. Закройте окно, чтобы отменить.
ping -n 11 127.0.0.1 >nul

:start_crawl
set "QUICKFAILS=0"
:loop
call :now T0
%PY% -m azbyka_reserv crawl --data "%DATA%"
set "RC=%errorlevel%"
if "%RC%"=="0" goto finished
if "%RC%"=="3" goto more_work
if "%RC%"=="4" goto diskfull
if "%RC%"=="5" goto tls
if "%RC%"=="6" goto busy
if "%RC%"=="130" goto stopped
rem Ctrl+C до того, как программа успела его обработать.
if "%RC%"=="-1073741510" goto stopped
rem Прочие коды (1 — сбой с трассировкой и т. п.): перезапуск через минуту.
call :now T1
set "ELAPSED=999"
if defined T0 if defined T1 set /a "ELAPSED=T1-T0"
set "QUICK=0"
if %ELAPSED% GEQ 0 if %ELAPSED% LSS 20 set "QUICK=1"
if "%QUICK%"=="1" (set /a "QUICKFAILS+=1") else set "QUICKFAILS=0"
if %QUICKFAILS% GEQ 2 goto startup_error
echo.
echo Сбор прерван (код %RC%). Перезапуск через 60 секунд. Закройте окно, чтобы остановить.
rem ping вместо timeout: timeout не работает без консоли (Планировщик заданий).
ping -n 61 127.0.0.1 >nul
goto loop

:more_work
set "QUICKFAILS=0"
echo.
echo Сеанс сбора закончен, но работа ещё осталась. Продолжаем через 10 секунд...
ping -n 11 127.0.0.1 >nul
goto loop

:finished
echo.
echo ===== Сбор завершён: очередь пуста =====
%PY% -m azbyka_reserv status --data "%DATA%"
set "RC=0"
pause
goto quit

:diskfull
echo.
echo Остановлено: заканчивается место на диске. Освободите место или укажите другой диск.
pause
goto quit

:tls
echo.
echo Остановлено: не удаётся проверить сертификат сайта (ошибка SSL/TLS).
echo Установите пакеты сертификатов — выполните в командной строке:
echo     py -3 -m pip install truststore certifi
if /i not "%PY%"=="py -3" echo   или, если команды py нет:  %PY% -m pip install truststore certifi
echo и запустите zapusk.bat снова — программа будет использовать их автоматически.
echo Также проверьте, что на компьютере правильно установлены дата и время.
pause
goto quit

:busy
echo.
echo Остановлено: сбор в эту папку уже запущен в другом окне.
echo Второй запуск не нужен. Если другого окна нет, подождите минуту и запустите снова.
pause
goto quit

:startup_error
echo.
echo Программа дважды подряд завершилась с ошибкой (код %RC%) сразу после запуска.
echo Похоже на ошибку запуска: неверный config.toml, недоступная папка архива или диск,
echo повреждённая установка. Прочитайте сообщение об ошибке выше, исправьте причину
echo и запустите zapusk.bat снова. Журнал: "%DATA_ABS%\logs"
pause
goto quit

:stopped
echo Остановлено пользователем.
set "RC=130"
goto quit

:quit
popd
exit /b %RC%

:nodir
echo Не удалось открыть папку программы: "%~dp0"
pause
exit /b 1

:now
rem Текущее время в секундах (через Python — не зависит от региональных настроек).
set "%~1="
for /f %%t in ('%PY% -c "import time; print(int(time.time()))" 2^>nul') do set "%~1=%%t"
exit /b 0

:check_onedrive
rem Если DATA_ABS лежит внутри папки OneDrive (параметр 1), запоминает её в IN_ONEDRIVE.
rem Замена подстроки в cmd не различает регистр — сравнение без учёта регистра.
set "OD=%~1"
if not defined OD exit /b 0
if "%OD:~-1%"=="\" set "OD=%OD:~0,-1%"
set "OD_HAY=?%DATA_ABS%\"
call set "OD_REST=%%OD_HAY:?%OD%\=%%"
if not "%OD_REST%"=="%OD_HAY%" set "IN_ONEDRIVE=%OD%"
exit /b 0

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
