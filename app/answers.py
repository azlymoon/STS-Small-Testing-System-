"""Задачи с кратким ответом (тесты): нормализация и проверка ответов без запуска кода.

Ответы задачи хранятся в tasks.answers как JSON: [{"label": "X₂", "accept": ["10000"]}, ...] —
одно поле ввода на элемент, в accept — все допустимые варианты.
Поле с выбором нескольких вариантов: {"type": "multi", "label": "...", "choices": [...], "accept": [верные варианты]} —
верно, только если отмечены ровно все верные варианты.
"""
import json
import re

_SUB = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
# кириллица, похожая на латиницу (ученики путают раскладку в 16-ричных числах)
_LOOKALIKE = str.maketrans("АВСЕНКМОРТХавсенкмортх", "ABCEHKMOPTXABCEHKMOPTX")


def _num(s):
    """Число в любой системе: без пробелов, индекса основания, ведущих нулей; верхний регистр."""
    s = s.strip().translate(_LOOKALIKE).upper()
    s = re.sub(r"[₀₁₂₃₄₅₆₇₈₉]+$", "", s)            # 9D₁₆
    s = re.sub(r"\s*(\(\d+\)|_\d+)$", "", s)          # 9D(16), 9D_16
    s = re.sub(r"\s+", "", s)
    return s.lstrip("0") or ("0" if s else "")


def _text(s):
    s = s.strip().lower().replace("ё", "е")
    s = re.sub(r"[.,;:!?«»\"'()]+", " ", s)
    return " ".join(s.split())


def _numbers(s):
    return ",".join(re.findall(r"-?\d+", s.translate(_SUB)))


def matches(given, accepted):
    if not given.strip():
        return False
    for a in accepted:
        if _num(given) == _num(a) or _text(given) == _text(a):
            return True
        if len(re.findall(r"\d+", a)) >= 2 and _numbers(given) == _numbers(a):  # пары вида (334, 375)
            return True
    return False


SEP = " | "  # разделитель выбранных вариантов в сохранённом ответе


def is_multi(f):
    return f.get("type") == "multi"


def fields(task):
    try:
        return json.loads(task["answers"] or "[]")
    except (ValueError, TypeError):
        return []


def check(task, given):
    """given — список ответов ученика по полям. Возвращает (verdict, passed, total, details)."""
    fs = fields(task)
    details, passed = [], 0
    for i, f in enumerate(fs):
        g = (given[i] if i < len(given) else "").strip()[:2000]
        if is_multi(f):
            chosen = {c for c in g.split(SEP) if c}
            ok = bool(chosen) and chosen == set(f["accept"])
        else:
            ok = matches(g[:500], f["accept"])
        passed += ok
        details.append({"n": i + 1, "label": f.get("label", ""), "got": g, "verdict": "OK" if ok else "WA"})
    return ("OK" if fs and passed == len(fs) else "WA"), passed, len(fs), details


def as_text(task, given):
    """Читаемая запись ответов — хранится вместо «кода» посылки."""
    fs = fields(task)
    return "\n".join(f"{f.get('label') or i + 1}: {given[i] if i < len(given) else ''}" for i, f in enumerate(fs))


def to_editable(answers_json):
    """JSON → текст для админки: «подпись = вариант1 | вариант2» по строке на поле."""
    try:
        fs = json.loads(answers_json or "[]")
    except ValueError:
        return answers_json or ""
    lines = []
    for f in fs:
        if is_multi(f):  # «подпись ?= *верный | неверный | *верный»
            lines.append(f"{f.get('label', '')} ?= " + " | ".join(("*" if c in f["accept"] else "") + c for c in f["choices"]))
        else:
            lines.append(f"{f.get('label', '')} = {' | '.join(f['accept'])}")
    return "\n".join(lines)


def from_editable(text):
    fs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if "?=" in line:
            label, _, rest = line.partition("?=")
            choices = [c.strip() for c in rest.split("|") if c.strip()]
            fs.append({"type": "multi", "label": label.strip(), "choices": [c.lstrip("*").strip() for c in choices],
                       "accept": [c.lstrip("*").strip() for c in choices if c.startswith("*")]})
            continue
        label, _, acc = line.rpartition("=") if "=" in line else ("", "", line)
        fs.append({"label": label.strip(), "accept": [a.strip() for a in acc.split("|") if a.strip()]})
    return json.dumps(fs, ensure_ascii=False)
