"""Стресс-тест: N учеников одновременно входят, открывают задачу демо-урока, отправляют решения и ждут вердикт.

Запуск: .venv/bin/python tools/stress_test.py <адрес сервера> [N] [префикс имён]
Нужен демо-урок (tools/seed_demo.py). Решения: верные / неверные / TLE / CE / RE — проверяется, что вердикты
совпали с ожидаемыми и (если занимаются несколько групп) посылки попали в группу, выбранную учеником.
Создаёт учеников «<префикс>NN Ученик» — НЕ запускайте на рабочем сервере во время занятий (см. README).
"""
import asyncio
import re
import sqlite3
import sys
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("MSHP_DATA") or ROOT / "data") / "mshp.sqlite3"  # та же база, что у сервера
BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 40
PREFIX = sys.argv[3] if len(sys.argv) > 3 else "Стресс"  # имена учеников: «<PREFIX>NN Ученик»

# Решения для задач демо-урока (tools/seed_demo.py), slug "demo"
SUM_OK = "#include <iostream>\nint main(){ long long a, b; std::cin >> a >> b; std::cout << a + b; }\n"
SUM_WA = "#include <iostream>\nint main(){ long long a, b; std::cin >> a >> b; std::cout << a - b; }\n"
MAX_OK = (ROOT / "examples/demo/array_max/sol.cpp").read_text()
MAX_WA = "#include <iostream>\nint main(){ int n; long long x; std::cin >> n >> x; std::cout << x; }\n"  # первый элемент
TLE = "#include <iostream>\nint main(){ volatile long long x = 0; while (true) x++; }\n"
CE = "#include <iostream>\nint main(){ std::cout << y; }\n"
RE = "#include <iostream>\nint main(){ int* p = nullptr; *p = 1; }\n"

con = sqlite3.connect(DB)
tid = lambda title: con.execute("SELECT t.id, l.slug FROM tasks t JOIN lessons l ON l.id=t.lesson_id "
                                "WHERE l.slug='demo' AND t.title=?", (title,)).fetchone()
SUM, MAX = tid("Сумма двух чисел"), tid("Максимум в массиве")
if not (SUM and MAX):
    sys.exit("Нет демо-урока: сначала запустите  .venv/bin/python tools/seed_demo.py")

PLAN = [  # (задача, код, ожидаемый вердикт)
    (SUM, SUM_OK, "OK"), (SUM, SUM_WA, "WA"), (SUM, TLE, "TLE"), (SUM, CE, "CE"), (SUM, RE, "RE"),
    (MAX, MAX_OK, "OK"), (MAX, MAX_WA, "WA"), (MAX, TLE, "TLE"), (MAX, MAX_OK, "OK"), (MAX, CE, "CE"),
]

stats = {"requests": 0, "errors": 0, "lat": []}


async def req(client, method, url, **kw):
    for attempt in range(5):
        t = time.time()
        try:
            r = await client.request(method, url, **kw)
            stats["requests"] += 1
            stats["lat"].append(time.time() - t)
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
            return r
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            print(f"  ! {method} {url}: {e!r} (попытка {attempt + 1})")
            await asyncio.sleep(1 + attempt)
    raise RuntimeError(f"не удалось: {method} {url}")


async def student(i, start_evt):
    (task_id, slug), code, expected = PLAN[i % len(PLAN)]
    name = f"{PREFIX}{i:02d} Ученик"
    async with httpx.AsyncClient(base_url=BASE, timeout=30, follow_redirects=False) as c:
        await req(c, "GET", f"/lesson/{slug}")
        await req(c, "POST", "/login", data={"name": name, "next": f"/lesson/{slug}"})
        group_id = None
        for _ in range(3):  # первый вход: выбор года, затем (если групп несколько) выбор группы
            r = await req(c, "GET", f"/lesson/{slug}")
            loc = r.headers.get("location", "")
            if "/choose-year" in loc:
                grp = con.execute("SELECT grp FROM lessons WHERE slug=?", (slug,)).fetchone()[0]
                await req(c, "POST", "/choose-year", data={"grp": grp, "next": f"/lesson/{slug}"})
            elif "/choose-group" in loc:
                page = await req(c, "GET", loc)
                opts = re.findall(r'name="group_id" value="(\d+)"', page.text)
                group_id = int(opts[i % len(opts)])
                await req(c, "POST", "/choose-group", data={"group_id": group_id, "next": f"/lesson/{slug}"})
            else:
                break
        r = await req(c, "GET", f"/lesson/{slug}")
        assert name in r.text, f"{name}: не вошёл"
        await req(c, "GET", f"/lesson/{slug}/task/{task_id}")
        await start_evt.wait()  # все отправляют одновременно
        t0 = time.time()
        r = await req(c, "POST", f"/lesson/{slug}/task/{task_id}/submit", data={"code": code})
        loc = r.headers.get("location", "")
        m = re.search(r"/submission/(\d+)", loc)
        assert m, f"{name}: посылка не принята, redirect={loc}"
        sid = m[1]
        while True:
            await asyncio.sleep(2)  # как meta refresh на странице
            r = await req(c, "GET", f"/submission/{sid}")
            if 'http-equiv="refresh"' not in r.text:
                break
            if time.time() - t0 > 600:
                return name, expected, "TIMEOUT", time.time() - t0, sid, group_id
        card = r.text[r.text.index("Посылка #"):]  # вердикт в карточке посылки, а не плашка группы в шапке
        v = re.search(r'class="v v-(\w+)"', card)[1]
        return name, expected, v, time.time() - t0, sid, group_id


async def main():
    print(f"Сервер: {BASE}, учеников: {N}")
    evt = asyncio.Event()
    tasks = [asyncio.create_task(student(i, evt)) for i in range(N)]
    await asyncio.sleep(8)  # все успевают войти и открыть задачу
    t0 = time.time()
    evt.set()
    res = await asyncio.gather(*tasks, return_exceptions=True)
    total = time.time() - t0
    bad = 0
    for x in res:
        if isinstance(x, Exception):
            bad += 1
            print("ОШИБКА:", x)
        elif x[1] != x[2]:
            bad += 1
            print(f"НЕСОВПАДЕНИЕ: {x[0]} ожидали {x[1]}, получили {x[2]}")
    # посылки должны попасть в ту группу, которую выбрал ученик
    for x in res:
        if isinstance(x, Exception) or x[5] is None:
            continue
        got = sqlite3.connect(DB).execute("SELECT group_id FROM submissions WHERE id=?", (x[4],)).fetchone()[0]
        if got != x[5]:
            bad += 1
            print(f"НЕ ТА ГРУППА: {x[0]} выбрал {x[5]}, посылка в {got}")
    chosen = [x[5] for x in res if not isinstance(x, Exception) and x[5]]
    if chosen:
        print("Группы учеников:", {g: chosen.count(g) for g in sorted(set(chosen))})
    times = sorted(x[3] for x in res if not isinstance(x, Exception))
    lat = sorted(stats["lat"])
    print(f"\nГотово за {total:.1f} с. Верно: {N - bad}/{N}")
    if times:
        print(f"Время до вердикта: медиана {times[len(times) // 2]:.1f} с, макс {times[-1]:.1f} с")
    print(f"HTTP-запросов: {stats['requests']}, ошибок/повторов: {stats['errors']}, "
          f"задержка медиана {lat[len(lat) // 2] * 1000:.0f} мс, p95 {lat[int(len(lat) * .95)] * 1000:.0f} мс")
    sys.exit(1 if bad else 0)


asyncio.run(main())
