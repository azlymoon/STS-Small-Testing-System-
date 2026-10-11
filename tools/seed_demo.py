"""Создаёт демо-урок (/lesson/demo) с двумя задачами и тестами — чтобы попробовать систему без своих задач.

    .venv/bin/python tools/seed_demo.py
Повторный запуск ничего не дублирует. База: data/ или MSHP_DATA.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

TASKS = [
    ("Сумма двух чисел", """Даны два целых числа **a** и **b**. Выведите их сумму.

### Формат входных данных

Два целых числа через пробел, по модулю не больше 10<sup>9</sup>.

### Формат выходных данных

Одно целое число — сумма.""", [("2 3\n", "5\n"), ("-4 10\n", "6\n")]),
    ("Максимум в массиве", """Дан массив из **n** целых чисел. Выведите наибольший элемент.

### Формат входных данных

В первой строке — **n** (1 ≤ n ≤ 10<sup>5</sup>). Во второй — **n** целых чисел, по модулю не больше 10<sup>9</sup>.

### Формат выходных данных

Одно число — максимум.

### Примечания

```cpp
#include <iostream>
using namespace std;

int main() {
    int n;
    cin >> n;
    // ваш код
}
```""", [("5\n3 9 -1 9 2\n", "9\n"), ("1\n-7\n", "-7\n")]),
]


def main():
    db.init()
    con = db.connect()
    if not con.execute("SELECT 1 FROM lessons WHERE slug='demo'").fetchone():
        con.execute("INSERT INTO lessons(slug, title, grp) VALUES('demo', 'Демо · Первые задачи', 'Демо')")
    lid = con.execute("SELECT id FROM lessons WHERE slug='demo'").fetchone()["id"]
    for pos, (title, statement, samples) in enumerate(TASKS, 1):
        if con.execute("SELECT 1 FROM tasks WHERE lesson_id=? AND title=?", (lid, title)).fetchone():
            continue
        tid = con.execute("INSERT INTO tasks(lesson_id, pos, title, statement) VALUES(?,?,?,?)",
                          (lid, pos, title, statement)).lastrowid
        for i, (inp, out) in enumerate(samples):
            con.execute("INSERT INTO tests(task_id, pos, input, output, is_sample) VALUES(?,?,?,?,1)", (tid, i, inp, out))
    con.close()
    from tools.gen_tests import main as gen
    gen(ROOT / "examples" / "demo", [])
    print("Демо-урок готов: /lesson/demo")


if __name__ == "__main__":
    main()
