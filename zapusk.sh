#!/usr/bin/env bash
# Сбор архива azbyka.ru (Linux/macOS). Повторный запуск продолжает с места остановки.
# Использование: ./zapusk.sh [папка_для_архива]
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Нужен Python 3.11+"; exit 1; }
DATA=${1:-data}
echo "Архив: $DATA"
while true; do
  "$PY" -m azbyka_reserv crawl --data "$DATA"
  rc=$?
  case $rc in
    0) echo "Сбор завершён."; "$PY" -m azbyka_reserv status --data "$DATA"; exit 0;;
    4) echo "Мало места на диске — остановлено."; exit 4;;
    130) echo "Остановлено пользователем."; exit 130;;
    *) echo "Прервано (код $rc). Перезапуск через 60 с (Ctrl+C — стоп)."; sleep 60 || exit 130;;
  esac
done
