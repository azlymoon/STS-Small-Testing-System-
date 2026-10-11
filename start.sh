#!/bin/sh
# Запуск всего: сервер + публичная ссылка через Cloudflare. Остановка: Ctrl+C
cd "$(dirname "$0")"
pkill -f "uvicorn app.main:app" 2>/dev/null
pkill -f "cloudflared tunnel --no-autoupdate" 2>/dev/null
./run.sh > data/server.log 2>&1 &
SERVER=$!
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 > data/tunnel.log 2>&1 &
TUNNEL=$!
trap 'kill $SERVER $TUNNEL 2>/dev/null; exit' INT TERM
printf "Жду ссылку"
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' data/tunnel.log | head -1)
  [ -n "$URL" ] && break
  printf "."; sleep 1
done
echo
SHORT=$(curl -s -m 10 "https://clck.ru/--?url=$URL/" | grep -o 'clck\.ru/[A-Za-z0-9]*')
echo "$URL" > data/public_url.txt
echo "${SHORT:-$URL}" > data/short_url.txt
echo "================================================================"
echo " ДЛЯ УЧЕНИКОВ:      ${SHORT:-$URL}"
echo " Показать на экране: $URL/screen"
echo " Полная ссылка:     $URL"
echo " Админка:           $URL/admin   (пароль: data/admin_password.txt)"
echo " Локально:          http://127.0.0.1:8000"
echo "================================================================"
wait
