"""Демо-набор: генераторы тестов для двух задач демо-урока (tools/seed_demo.py).

Используется как образец: скопируйте в tests_src/generators.py и опишите свои задачи.
    TITLES[key] — название задачи в системе ("=" в начале — точное совпадение)
    GENS[key]   — функция, возвращающая список входных данных (строк)
    <key>/sol.cpp — эталонное решение
"""
import random

TITLES = {
    "sum_two": "=Сумма двух чисел",
    "array_max": "=Максимум в массиве",
}


def g_sum_two():
    rows = [(0, 0), (1, -1), (-5, -7), (10**9, 10**9), (-10**9, 10**9)]
    rows += [(random.randint(-1000, 1000), random.randint(-1000, 1000)) for _ in range(5)]
    return [f"{a} {b}\n" for a, b in rows]


def g_array_max():
    out = ["1\n5\n", "3\n-1 -2 -3\n", "4\n7 7 7 7\n", "5\n1 2 3 4 5\n", "5\n5 4 3 2 1\n"]
    for n in (10, 100, 1000, 100000):
        out.append(f"{n}\n{' '.join(str(random.randint(-10**9, 10**9)) for _ in range(n))}\n")
    return out


GENS = {"sum_two": g_sum_two, "array_max": g_array_max}
