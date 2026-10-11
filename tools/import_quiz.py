"""Импорт урока-теста (задачи с кратким ответом) из JSON.

    .venv/bin/python tools/import_quiz.py tests_src/quizzes/<урок>.json

Формат (пример — examples/quiz_example.json):
    {"lesson": {"slug": "dm-l05", "title": "Занятие 5 · ...", "grp": "ДМ 1 год"},
     "sections": [{"name": "Тест 1", "tasks": [
         {"title": "Вопрос 1", "statement": "Markdown", "fields": [{"label": "X₂", "accept": ["1010"]}]}]}]}

Каждая секция — вкладка урока. Повторный импорт обновляет условия и ответы (по вкладке и названию),
добавляет новые вопросы и не трогает посылки. База: data/ или MSHP_DATA.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


def main(path):
    data = json.loads(Path(path).read_text())
    les = data["lesson"]
    db.init()
    con = db.connect()
    row = con.execute("SELECT id FROM lessons WHERE slug=?", (les["slug"],)).fetchone()
    if row:
        lid = row["id"]
        con.execute("UPDATE lessons SET title=?, grp=? WHERE id=?", (les["title"], les.get("grp", ""), lid))
    else:
        lid = con.execute("INSERT INTO lessons(slug, title, grp) VALUES(?,?,?)",
                          (les["slug"], les["title"], les.get("grp", ""))).lastrowid
    pos = created = updated = 0
    for sec in data["sections"]:
        for t in sec["tasks"]:
            pos += 1
            answers = json.dumps(t["fields"], ensure_ascii=False)
            old = con.execute("SELECT id FROM tasks WHERE lesson_id=? AND section=? AND title=?",
                              (lid, sec["name"], t["title"])).fetchone()
            if old:
                con.execute("UPDATE tasks SET statement=?, answers=?, pos=?, kind='answer' WHERE id=?",
                            (t["statement"], answers, pos, old["id"]))
                updated += 1
            else:
                con.execute("INSERT INTO tasks(lesson_id, pos, title, statement, kind, section, answers) "
                            "VALUES(?,?,?,?,'answer',?,?)", (lid, pos, t["title"], t["statement"], sec["name"], answers))
                created += 1
    con.close()
    print(f"Урок /lesson/{les['slug']}: создано вопросов {created}, обновлено {updated}")


if __name__ == "__main__":
    main(sys.argv[1])
