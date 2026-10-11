import re
import sqlite3
from pathlib import Path

import os

# MSHP_DATA — другая папка данных (для тестов на копии базы, не трогая рабочую)
DATA = Path(os.environ.get("MSHP_DATA") or Path(__file__).resolve().parent.parent / "data")
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "mshp.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    is_open INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    statement TEXT NOT NULL DEFAULT '',
    time_limit REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL DEFAULT 0,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    is_sample INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT UNIQUE NOT NULL,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued / running / done
    verdict TEXT,                            -- OK WA TLE RE CE SE
    passed INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    compile_log TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    grp TEXT NOT NULL DEFAULT '',            -- год (вкладка), например «1 год»
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS sub_status ON submissions(status);
CREATE INDEX IF NOT EXISTS sub_student_task ON submissions(student_id, task_id);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init():
    con = connect()
    con.executescript(SCHEMA)
    cols = [r["name"] for r in con.execute("PRAGMA table_info(lessons)")]
    if "grp" not in cols:  # вкладка «1 год / 2 год»
        con.execute("ALTER TABLE lessons ADD COLUMN grp TEXT NOT NULL DEFAULT ''")
        for r in con.execute("SELECT id, title FROM lessons").fetchall():
            m = re.match(r"\s*(\d+\s*год)", r["title"])
            if m:
                con.execute("UPDATE lessons SET grp=? WHERE id=?", (m[1], r["id"]))
    for table, col, ddl in (("submissions", "judged_at", "TEXT"), ("students", "last_seen", "TEXT"),
                            ("submissions", "group_id", "INTEGER REFERENCES groups(id) ON DELETE SET NULL"),
                            ("students", "group_id", "INTEGER REFERENCES groups(id) ON DELETE SET NULL"),
                            ("groups", "is_active", "INTEGER NOT NULL DEFAULT 0"),
                            ("students", "grp", "TEXT NOT NULL DEFAULT ''"),
                            ("tasks", "kind", "TEXT NOT NULL DEFAULT 'code'"),     # code — программа, answer — краткий ответ
                            ("tasks", "section", "TEXT NOT NULL DEFAULT ''"),      # вкладка внутри урока
                            ("tasks", "answers", "TEXT NOT NULL DEFAULT ''")):     # ответы для kind=answer (JSON)
        if col not in [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            if col == "is_active":  # раньше активная группа была одна и хранилась в settings
                r = con.execute("SELECT value FROM settings WHERE key='active_group'").fetchone()
                if r and r["value"]:
                    con.execute("UPDATE groups SET is_active=1 WHERE id=?", (int(r["value"]),))
            if table == "students" and col == "grp":  # год уже входивших учеников: по группе или по их урокам
                con.execute("""UPDATE students SET grp=(SELECT g.grp FROM groups g WHERE g.id=students.group_id)
                               WHERE group_id IS NOT NULL AND (SELECT g.grp FROM groups g WHERE g.id=students.group_id) != ''""")
                con.execute("""UPDATE students SET grp=(
                                   SELECT MAX(l.grp) FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN lessons l ON l.id=t.lesson_id
                                   WHERE s.student_id=students.id AND l.grp!='')
                               WHERE grp='' AND (SELECT COUNT(DISTINCT l.grp) FROM submissions s JOIN tasks t ON t.id=s.task_id
                                   JOIN lessons l ON l.id=t.lesson_id WHERE s.student_id=students.id AND l.grp!='') = 1""")
    con.close()


def get_setting(key, default=None):
    con = connect()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()


def set_setting(key, value):
    con = connect()
    try:
        con.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    finally:
        con.close()


def active_groups():
    """Группы, которые занимаются прямо сейчас (их может быть несколько)."""
    con = connect()
    try:
        return con.execute("SELECT * FROM groups WHERE is_active=1 AND archived=0 ORDER BY grp, name").fetchall()
    finally:
        con.close()
