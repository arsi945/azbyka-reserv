@echo off
rem Сбор архива azbyka.ru. Можно закрывать окно и запускать снова — продолжит с места остановки.
rem Использование: zapusk.bat [папка_для_архива]   (по умолчанию .\data)
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call :find_python || exit /b 1
set "DATA=%~dp0data"
if not "%~1"=="" set "DATA=%~1"
echo Архив будет сохраняться в: %DATA%
:loop
%PY% -m azbyka_reserv crawl --data "%DATA%"
set RC=%errorlevel%
if %RC%==0 goto finished
if %RC%==4 goto diskfull
if %RC%==130 goto stopped
echo.
echo Сбор прерван (код %RC%). Перезапуск через 60 секунд. Закройте окно, чтобы остановить.
timeout /t 60 >nul
goto loop
:finished
echo.
echo ===== Сбор завершён: очередь пуста =====
%PY% -m azbyka_reserv status --data "%DATA%"
pause
exit /b 0
:diskfull
echo.
echo Остановлено: заканчивается место на диске. Освободите место или укажите другой диск.
pause
exit /b 4
:stopped
echo Остановлено пользователем.
exit /b 130

:find_python
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto oldpython
exit /b 0
:nopython
echo Не найден Python. Установите Python 3.11 или новее с https://www.python.org/downloads/
echo (при установке отметьте галочку "Add python.exe to PATH") и запустите этот файл снова.
pause
exit /b 1
:oldpython
echo Установлен слишком старый Python. Нужен 3.11 или новее: https://www.python.org/downloads/
pause
exit /b 1
