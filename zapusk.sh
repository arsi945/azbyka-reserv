#!/usr/bin/env bash
# Сбор архива azbyka.ru (Linux/macOS). Повторный запуск продолжает с места остановки.
# Использование: ./zapusk.sh [папка_для_архива]   (по умолчанию ./data рядом со скриптом)
# Другой Python: PYTHON=python3.12 ./zapusk.sh
#
# Коды выхода crawl: 0 — всё скачано; 3 — работа осталась, перезапуск; 4 — мало места;
# 5 — не проходит проверка сертификата TLS; 6 — сбор уже запущен для этой папки;
# 130 — остановлено пользователем (Ctrl+C); прочие (1 — сбой) — перезапуск через минуту,
# но два быстрых (< 20 с) сбоя подряд считаем ошибкой запуска и останавливаемся.
cd "$(dirname "$0")" || { echo "Не удалось перейти в папку программы: $(dirname "$0")" >&2; exit 1; }
export PYTHONUTF8=1
PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Не найден Python ($PY). Установите Python 3.11 или новее и запустите снова." >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  echo "Нужен Python 3.11 или новее, а $PY — это $("$PY" -V 2>&1)." >&2
  echo "Установите новее и укажите его, например: PYTHON=python3.12 ./zapusk.sh" >&2
  exit 1
fi
DATA=${1:-data}
echo "Архив: $DATA"

quick_fails=0
while true; do
  started=$(date +%s)
  "$PY" -m azbyka_reserv crawl --data "$DATA"
  rc=$?
  case $rc in
    0)
      echo
      echo "===== Сбор завершён: очередь пуста ====="
      "$PY" -m azbyka_reserv status --data "$DATA"
      exit 0;;
    3)
      quick_fails=0
      echo
      echo "Сеанс сбора закончен, но работа ещё осталась. Продолжаем через 10 с (Ctrl+C — стоп)."
      sleep 10 || exit 130;;
    4)
      echo
      echo "Остановлено: заканчивается место на диске. Освободите место или укажите другой диск."
      exit 4;;
    5)
      echo
      echo "Остановлено: не удаётся проверить сертификат сайта (ошибка SSL/TLS)."
      echo "Установите пакеты сертификатов:"
      echo "    $PY -m pip install truststore certifi"
      echo "(или системный пакет ca-certificates) и запустите снова —"
      echo "программа будет использовать их автоматически. Проверьте также дату и время."
      exit 5;;
    6)
      echo
      echo "Остановлено: сбор в эту папку уже запущен в другом окне/процессе."
      echo "Второй запуск не нужен. Если другого запуска нет, подождите минуту и запустите снова."
      exit 6;;
    130)
      echo "Остановлено пользователем."
      exit 130;;
    *)
      elapsed=$(( $(date +%s) - started ))
      if [ "$elapsed" -ge 0 ] && [ "$elapsed" -lt 20 ]; then
        quick_fails=$((quick_fails + 1))
      else
        quick_fails=0
      fi
      if [ "$quick_fails" -ge 2 ]; then
        echo
        echo "Программа дважды подряд завершилась с ошибкой (код $rc) сразу после запуска."
        echo "Похоже на ошибку запуска: неверный config.toml, недоступная папка архива или диск."
        echo "Прочитайте сообщение об ошибке выше, исправьте причину и запустите снова."
        exit "$rc"
      fi
      echo
      echo "Сбор прерван (код $rc). Перезапуск через 60 с (Ctrl+C — стоп)."
      sleep 60 || exit 130;;
  esac
done
