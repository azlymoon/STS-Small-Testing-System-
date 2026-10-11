"""Генерация тестов по эталонному решению.

Набор задач описывается модулем generators.py в папке набора (по умолчанию tests_src/ — приватная папка,
в git не попадает; пример — examples/demo/):

    TITLES = {"key": "Название задачи"}   # как задача называется в системе; "=..." — точное совпадение,
                                          # иначе поиск по подстроке
    GENS   = {"key": lambda: [вход1, вход2, ...]}   # функции, возвращающие список входов (строки)

    <папка набора>/<key>/sol.cpp          # эталонное решение

Для каждой задачи: эталон компилируется, проверяется на примерах из условия (тесты is_sample=1),
по каждому входу получается правильный ответ. Тесты сохраняются в <папка>/<key>/tests/NN.in|.out
и загружаются в БД (все НЕ-примеры задачи заменяются).

Запуск:
    .venv/bin/python tools/gen_tests.py                          # все задачи из tests_src/
    .venv/bin/python tools/gen_tests.py key1 key2                # выбранные задачи
    .venv/bin/python tools/gen_tests.py --set examples/demo      # другой набор
База: data/mshp.sqlite3 или папка из переменной MSHP_DATA.
"""
import importlib.util
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("MSHP_DATA") or ROOT / "data") / "mshp.sqlite3"


def load_set(path):
    spec = importlib.util.spec_from_file_location("generators", path / "generators.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TITLES, mod.GENS


def build(src, key):
    exe = src / key / "sol"
    subprocess.run(["g++", "-std=c++17", "-O2", "-o", exe, src / key / "sol.cpp"], check=True)
    return exe


def run(exe, inp):
    return subprocess.run([exe], input=inp, capture_output=True, text=True, check=True, timeout=10).stdout


def find_task(con, title):
    if title.startswith("="):
        return con.execute("SELECT id FROM tasks WHERE title=? ORDER BY id DESC", (title[1:],)).fetchone()
    return con.execute("SELECT id FROM tasks WHERE title LIKE ? ORDER BY id DESC", (f"%{title}%",)).fetchone()


def main(src, keys):
    sys.path.insert(0, str(ROOT))
    from app.judge import outputs_match

    titles, gens = load_set(src)
    con = sqlite3.connect(DB, isolation_level=None)
    for key in keys or list(gens):
        random.seed(sum(map(ord, key)))  # тесты воспроизводимы
        row = find_task(con, titles[key])
        if not row:
            print(f"!! {key}: задача «{titles[key]}» не найдена в БД — сначала загрузите урок")
            continue
        tid = row[0]
        exe = build(src, key)
        try:
            # эталон обязан проходить примеры из условия
            for inp, out in con.execute("SELECT input, output FROM tests WHERE task_id=? AND is_sample=1", (tid,)).fetchall():
                got = run(exe, inp)
                assert outputs_match(out, got), f"{key}: эталон не прошёл пример\n{inp}\nожидалось {out}\nполучено {got}"
            tdir = src / key / "tests"
            tdir.mkdir(exist_ok=True)
            for f in tdir.glob("*"):
                f.unlink()
            con.execute("DELETE FROM tests WHERE task_id=? AND is_sample=0", (tid,))
            for i, inp in enumerate(gens[key](), 1):
                out = run(exe, inp)
                (tdir / f"{i:02d}.in").write_text(inp)
                (tdir / f"{i:02d}.out").write_text(out)
                con.execute("INSERT INTO tests(task_id, pos, input, output, is_sample) VALUES(?,?,?,?,0)", (tid, 100 + i, inp, out))
            n = con.execute("SELECT COUNT(*) FROM tests WHERE task_id=?", (tid,)).fetchone()[0]
            print(f"{key}: задача {tid}, тестов всего {n}")
        finally:
            exe.unlink(missing_ok=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    src = ROOT / "tests_src"
    if args[:1] == ["--set"]:
        src, args = (ROOT / args[1]).resolve(), args[2:]
    main(src, args)
