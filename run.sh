#!/bin/sh
# Запуск только сервера (без публичной ссылки): ./run.sh — http://127.0.0.1:8000
# Пароль админки — в data/admin_password.txt или env ADMIN_PASSWORD
cd "$(dirname "$0")"
docker image inspect mshp-cpp >/dev/null 2>&1 || docker build -t mshp-cpp sandbox
KEEP_AWAKE=""
command -v caffeinate >/dev/null 2>&1 && KEEP_AWAKE="caffeinate -i"   # macOS: не засыпать, пока работает сервер
exec $KEEP_AWAKE .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
