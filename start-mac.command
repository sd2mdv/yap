#!/bin/bash
# Двойной щелчок: запускает сервер курса. Окно не закрывайте, пока идёт занятие.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Не найден Python 3. Установите его: https://www.python.org/downloads/ (или xcode-select --install)"; read -r; exit 1
fi
python3 server.py "$@"
echo; echo "Нажмите Enter, чтобы закрыть окно"; read -r
