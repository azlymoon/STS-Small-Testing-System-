#!/bin/sh
# /box: main.cpp, tests/*.in ; results -> /box/out
cd /box
mkdir -p out
g++ -std=c++17 -O2 -pipe -o /tmp/a.out main.cpp 2> out/compile.txt
echo $? > out/compile.rc
[ "$(cat out/compile.rc)" = "0" ] || exit 0
TL=${TL:-2}
TL_MS=${TL_MS:-2000}
tle=0
for f in tests/*.in; do
  [ "$tle" -ge 3 ] && break   # после трёх TLE остальные тесты не запускаем
  n=$(basename "$f" .in)
  s=$(date +%s%N)
  timeout -s KILL "$TL" /tmp/a.out < "$f" > "out/$n.out" 2> "out/$n.err"
  rc=$?
  e=$(date +%s%N)
  kill -9 -1 2>/dev/null   # добить всё, что программа могла наплодить через fork
  ms=$(( (e - s) / 1000000 ))
  echo "$rc $ms" > "out/$n.rc"
  [ "$rc" -eq 137 ] && [ "$ms" -ge $(( TL_MS * 9 / 10 )) ] && tle=$((tle + 1))
done
