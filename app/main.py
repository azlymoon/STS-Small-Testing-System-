import asyncio
import io
import json
import os
import re
import secrets
import time
import zipfile
from urllib.parse import quote
from contextlib import asynccontextmanager

import markdown as md
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import dashboard, db, groups, judge
from .docx_import import parse_docx

db.init()


def _persisted(name, factory):
    p = db.DATA / name
    if not p.exists():
        p.write_text(factory())
    return p.read_text().strip()


SECRET = _persisted("secret.txt", lambda: secrets.token_hex(32))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or _persisted("admin_password.txt", lambda: secrets.token_urlsafe(9))
MAX_CODE = 64 * 1024
SUBMIT_COOLDOWN = 5  # секунд между посылками одного ученика


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(judge.worker_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(dashboard.router)
app.include_router(groups.router)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 180, same_site="lax")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["md"] = lambda s: md.markdown(s or "", extensions=["fenced_code", "tables", "nl2br"])
templates.env.filters["fromjson"] = json.loads
templates.env.filters["no_grp"] = lambda title, grp: re.sub(rf"^\s*{re.escape(grp)}\s*[·\-—:]\s*", "", title) if grp else title

VERDICTS = {
    "OK": "Принято", "WA": "Неверный ответ", "TLE": "Превышено время", "RE": "Ошибка выполнения", "ML": "Превышена память", "SK": "Не запускался",
    "CE": "Ошибка компиляции", "SE": "Ошибка системы", None: "В очереди",
}
templates.env.globals["VERDICTS"] = VERDICTS


def lesson_groups():
    return [r["grp"] for r in q("SELECT DISTINCT grp FROM lessons WHERE is_open=1 AND grp!='' ORDER BY grp")]


def render(request, template, **ctx):
    ctx.setdefault("student", current_student(request))
    ctx.setdefault("groups", lesson_groups())
    ctx.setdefault("cur_grp", request.session.get("grp", ""))
    ctx["is_admin"] = request.session.get("admin") is True
    ctx["active_groups"] = active = db.active_groups()
    ctx["my_group"] = student_group(ctx["student"], active)
    if ctx["is_admin"]:
        ctx["study_groups"] = q("SELECT * FROM groups WHERE archived=0 ORDER BY grp, name")
    return templates.TemplateResponse(request, template, ctx)


def q(sql, *args, one=False):
    con = db.connect()
    try:
        cur = con.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()
    finally:
        con.close()


def ex(sql, *args):
    con = db.connect()
    try:
        return con.execute(sql, args).lastrowid
    finally:
        con.close()


def name_key(name):
    return " ".join(name.lower().replace("ё", "е").split())


_seen = {}


def current_student(request):
    tok = request.session.get("student")
    st = q("SELECT * FROM students WHERE token=?", tok, one=True) if tok else None
    if st and time.time() - _seen.get(st["id"], 0) > 30:  # «в сети» для дашборда, не чаще раза в 30 с
        _seen[st["id"]] = time.time()
        ex("UPDATE students SET last_seen=datetime('now', 'localtime') WHERE id=?", st["id"])
    return st


def student_group(st, active=None):
    """Группа ученика среди занимающихся сейчас. Если занимается одна группа — она."""
    active = db.active_groups() if active is None else active
    if not st or not active:
        return None
    for g in active:
        if g["id"] == st["group_id"]:
            return g
    return active[0] if len(active) == 1 else None


def must_choose_group(st):
    """Занимаются несколько групп, а ученик ещё не выбрал свою."""
    active = db.active_groups()
    return bool(st and len(active) > 1 and student_group(st, active) is None)


def choose_group_redirect(request):
    return go("/choose-group?next=" + quote(request.url.path))


def require_admin(request):
    if request.session.get("admin") is not True:
        raise HTTPException(303, headers={"Location": "/admin/login"})


def go(url):
    return RedirectResponse(url, status_code=303)


def lesson_by_slug(slug, request):
    lesson = q("SELECT * FROM lessons WHERE slug=?", slug, one=True)
    if not lesson or (not lesson["is_open"] and not request.session.get("admin")):
        raise HTTPException(404, "Урок не найден")
    return lesson


def best_results(student_id, task_ids, group_id=0):
    """task_id -> лучшая посылка (по числу пройденных тестов). group_id: 0 — все, -1 — без группы."""
    if not task_ids:
        return {}
    gsql = "" if not group_id else " AND group_id IS NULL" if group_id == -1 else f" AND group_id={int(group_id)}"
    rows = q(
        f"SELECT * FROM submissions WHERE student_id=? AND task_id IN ({','.join('?' * len(task_ids))}){gsql} ORDER BY id",
        student_id, *task_ids,
    )
    res = {}
    for r in rows:
        b = res.get(r["task_id"])
        if b is None or r["verdict"] == "OK" or (b["verdict"] != "OK" and r["passed"] >= b["passed"]):
            res[r["task_id"]] = r
    return res


# ---------------------------------------------------------------- ученики

def short_url():
    p = db.DATA / "short_url.txt"
    return p.read_text().strip() if p.exists() else ""


@app.get("/screen", response_class=HTMLResponse)
def screen(request: Request):
    """Крупная ссылка для проектора."""
    return render(request, "screen.html", short=short_url() or str(request.base_url).rstrip("/"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request, g: str | None = None):
    if g is not None:
        request.session["grp"] = g
    grp = request.session.get("grp")
    if grp is None:  # ученик ещё не выбирал вкладку — открываем год его группы
        g = student_group(current_student(request))
        active = db.active_groups()
        grp = g["grp"] if g else (active[0]["grp"] if len(active) == 1 else "")
    groups = lesson_groups()
    if grp and grp not in groups:
        grp = ""
    lessons = q("SELECT * FROM lessons WHERE is_open=1 AND (?='' OR grp=?)", grp, grp)
    num = lambda l: int(m[1]) if (m := re.search(r"Занятие\s*(\d+)", l["title"])) else 0
    lessons = sorted(lessons, key=lambda l: (l["grp"], -num(l), l["id"]))  # свежие занятия сверху, внутри — по порядку
    return render(request, "index.html", lessons=lessons, groups=groups, cur_grp=grp)


@app.post("/login")
def login(request: Request, name: str = Form(""), next: str = Form("/"), group_id: int = Form(0)):
    name = " ".join(name.split())[:80]
    key = name_key(name)
    if len(key.split()) < 2:
        return render(request, "login.html", next=next, error="Введите фамилию и имя полностью", name=name)
    st = q("SELECT * FROM students WHERE name_key=?", key, one=True)
    if not st:
        ex("INSERT INTO students(name, name_key, token) VALUES(?,?,?)", name, key, secrets.token_hex(16))
        st = q("SELECT * FROM students WHERE name_key=?", key, one=True)
    request.session["student"] = st["token"]
    if group_id and any(g["id"] == group_id for g in db.active_groups()):
        ex("UPDATE students SET group_id=? WHERE id=?", group_id, st["id"])
    return go(next if next.startswith("/") else "/")


@app.get("/choose-group", response_class=HTMLResponse)
def choose_group_page(request: Request, next: str = "/"):
    if not current_student(request):
        return go("/")
    return render(request, "choose_group.html", next=next if next.startswith("/") else "/")


@app.post("/choose-group")
def choose_group(request: Request, group_id: int = Form(...), next: str = Form("/")):
    st = current_student(request)
    if st and any(g["id"] == group_id for g in db.active_groups()):
        ex("UPDATE students SET group_id=? WHERE id=?", group_id, st["id"])
        g = q("SELECT grp FROM groups WHERE id=?", group_id, one=True)
        if g and g["grp"]:
            request.session["grp"] = g["grp"]
    return go(next if next.startswith("/") else "/")


@app.get("/logout")
def logout(request: Request):
    request.session.pop("student", None)
    return go("/")


@app.get("/lesson/{slug}", response_class=HTMLResponse)
def lesson_page(request: Request, slug: str):
    lesson = lesson_by_slug(slug, request)
    st = current_student(request)
    if not st:
        return render(request, "login.html", next=request.url.path, lesson=lesson)
    if must_choose_group(st):
        return choose_group_redirect(request)
    if lesson["grp"]:
        request.session["grp"] = lesson["grp"]
    tasks = q("SELECT * FROM tasks WHERE lesson_id=? ORDER BY pos, id", lesson["id"])
    best = best_results(st["id"], [t["id"] for t in tasks])
    return render(request, "lesson.html", lesson=lesson, tasks=tasks, best=best)


@app.get("/lesson/{slug}/task/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, slug: str, task_id: int, error: str = ""):
    lesson = lesson_by_slug(slug, request)
    st = current_student(request)
    if not st:
        return render(request, "login.html", next=request.url.path, lesson=lesson)
    if must_choose_group(st):
        return choose_group_redirect(request)
    task = q("SELECT * FROM tasks WHERE id=? AND lesson_id=?", task_id, lesson["id"], one=True)
    if not task:
        raise HTTPException(404)
    tasks = q("SELECT id, title FROM tasks WHERE lesson_id=? ORDER BY pos, id", lesson["id"])
    samples = q("SELECT * FROM tests WHERE task_id=? AND is_sample=1 ORDER BY pos, id", task_id)
    subs = q("SELECT * FROM submissions WHERE student_id=? AND task_id=? ORDER BY id DESC", st["id"], task_id)
    last_code = subs[0]["code"] if subs else ""
    return render(request, "task.html", lesson=lesson, task=task, tasks=tasks, samples=samples, subs=subs,
                  last_code=last_code, error=error)


@app.post("/lesson/{slug}/task/{task_id}/submit")
async def submit(request: Request, slug: str, task_id: int, code: str = Form(""), file: UploadFile | None = File(None)):
    lesson = lesson_by_slug(slug, request)
    st = current_student(request)
    if not st:
        return go(f"/lesson/{slug}")
    if not q("SELECT 1 FROM tasks WHERE id=? AND lesson_id=?", task_id, lesson["id"], one=True):
        raise HTTPException(404)
    if file is not None and file.filename:
        code = (await file.read(MAX_CODE + 1)).decode("utf-8", errors="replace")
    back = f"/lesson/{slug}/task/{task_id}"
    if not code.strip():
        return go(back + "?error=Пустое+решение")
    if len(code) > MAX_CODE:
        return go(back + "?error=Слишком+большой+файл")
    last = request.session.get("last_submit", 0)
    if time.time() - last < SUBMIT_COOLDOWN:
        return go(back + "?error=Подождите+несколько+секунд+перед+следующей+отправкой")
    if q("SELECT COUNT(*) c FROM submissions WHERE student_id=? AND status!='done'", st["id"], one=True)["c"] >= 3:
        return go(back + "?error=Дождитесь+проверки+предыдущих+посылок")
    request.session["last_submit"] = time.time()
    if must_choose_group(st):
        return choose_group_redirect(request)
    ag = student_group(st)
    gid = ag["id"] if ag else None
    sid = ex("INSERT INTO submissions(student_id, task_id, code, group_id) VALUES(?,?,?,?)", st["id"], task_id, code, gid)
    if gid and st["group_id"] != gid:
        ex("UPDATE students SET group_id=? WHERE id=?", gid, st["id"])
    return go(f"/submission/{sid}")


@app.get("/submission/{sid}", response_class=HTMLResponse)
def submission_page(request: Request, sid: int):
    sub = q("SELECT s.*, st.name student_name, t.title task_title, t.lesson_id, l.slug FROM submissions s "
            "JOIN students st ON st.id=s.student_id JOIN tasks t ON t.id=s.task_id JOIN lessons l ON l.id=t.lesson_id "
            "WHERE s.id=?", sid, one=True)
    st = current_student(request)
    if not sub or not (request.session.get("admin") or (st and st["id"] == sub["student_id"])):
        raise HTTPException(404)
    details = json.loads(sub["details"])
    # у старых посылок вход/ответ хранились только для упавших тестов — берём из задачи
    tests = q("SELECT input, output FROM tests WHERE task_id=? ORDER BY is_sample DESC, pos, id", sub["task_id"])
    for d, t in zip(details, tests):
        d.setdefault("input", t["input"][:judge.MAX_SHOW])
        d.setdefault("expected", t["output"][:judge.MAX_SHOW])
    # соседние посылки этого ученика по этой задаче — для листания «← / →»
    ids = [r["id"] for r in q("SELECT id FROM submissions WHERE student_id=? AND task_id=? ORDER BY id",
                              sub["student_id"], sub["task_id"])]
    i = ids.index(sid)
    nav = {"n": i + 1, "total": len(ids), "prev": ids[i - 1] if i > 0 else None,
           "next": ids[i + 1] if i + 1 < len(ids) else None}
    return render(request, "submission.html", sub=sub, details=details, nav=nav)


# ---------------------------------------------------------------- админка

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return render(request, "admin_login.html")


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        request.session["admin"] = True
        return go("/admin")
    return render(request, "admin_login.html", error="Неверный пароль")


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.pop("admin", None)
    return go("/")


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    require_admin(request)
    lessons = q("SELECT l.*, (SELECT COUNT(*) FROM tasks t WHERE t.lesson_id=l.id) ntasks FROM lessons l ORDER BY id DESC")
    queue = q("SELECT COUNT(*) c FROM submissions WHERE status!='done'", one=True)["c"]
    all_groups = [r["grp"] for r in q("SELECT DISTINCT grp FROM lessons WHERE grp!='' ORDER BY grp")]
    return render(request, "admin_home.html", lessons=lessons, queue=queue, short=short_url(), all_groups=all_groups)


@app.post("/admin/lessons")
async def admin_create_lesson(request: Request, title: str = Form(...), slug: str = Form(""), grp: str = Form(""),
                              docx: UploadFile | None = File(None)):
    require_admin(request)
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower()) or secrets.token_hex(3)
    if q("SELECT 1 FROM lessons WHERE slug=?", slug, one=True):
        slug += "-" + secrets.token_hex(2)
    lid = ex("INSERT INTO lessons(slug, title, grp) VALUES(?,?,?)", slug, title.strip(), grp.strip())
    if docx is not None and docx.filename:
        import_docx_into(lid, io.BytesIO(await docx.read()))
    return go(f"/admin/lesson/{lid}")


def import_docx_into(lesson_id, fileobj):
    pos = q("SELECT COALESCE(MAX(pos), 0) m FROM tasks WHERE lesson_id=?", lesson_id, one=True)["m"]
    for t in parse_docx(fileobj):
        pos += 1
        tid = ex("INSERT INTO tasks(lesson_id, pos, title, statement) VALUES(?,?,?,?)", lesson_id, pos, t["title"], t["statement"])
        for i, (inp, out) in enumerate(t["examples"]):
            ex("INSERT INTO tests(task_id, pos, input, output, is_sample) VALUES(?,?,?,?,1)", tid, i, inp, out)


@app.get("/admin/lesson/{lid}", response_class=HTMLResponse)
def admin_lesson(request: Request, lid: int, group: int = 0):
    require_admin(request)
    lesson = q("SELECT * FROM lessons WHERE id=?", lid, one=True)
    if not lesson:
        raise HTTPException(404)
    tasks = q("SELECT t.*, (SELECT COUNT(*) FROM tests x WHERE x.task_id=t.id) ntests FROM tasks t WHERE lesson_id=? ORDER BY pos, id", lid)
    tids = [t["id"] for t in tasks]
    gsql = "" if not group else " AND s.group_id IS NULL" if group == -1 else f" AND s.group_id={int(group)}"
    students = q(
        "SELECT DISTINCT st.* FROM students st JOIN submissions s ON s.student_id=st.id JOIN tasks t ON t.id=s.task_id "
        f"WHERE t.lesson_id=?{gsql} ORDER BY st.name", lid)
    table = {st["id"]: best_results(st["id"], tids, group) for st in students}
    base = str(request.base_url).rstrip("/")
    all_groups = [r["grp"] for r in q("SELECT DISTINCT grp FROM lessons WHERE grp!='' ORDER BY grp")]
    return render(request, "admin_lesson.html", lesson=lesson, tasks=tasks, students=students, table=table, base=base,
                  all_groups=all_groups, group=group,
                  all_study_groups=q("SELECT * FROM groups ORDER BY archived, grp, name"))


@app.post("/admin/lesson/{lid}")
async def admin_lesson_update(request: Request, lid: int, title: str = Form(...), is_open: str = Form(""), grp: str = Form(""),
                              docx: UploadFile | None = File(None)):
    require_admin(request)
    ex("UPDATE lessons SET title=?, is_open=?, grp=? WHERE id=?", title, 1 if is_open else 0, grp.strip(), lid)
    if docx is not None and docx.filename:
        import_docx_into(lid, io.BytesIO(await docx.read()))
    return go(f"/admin/lesson/{lid}")


@app.post("/admin/lesson/{lid}/delete")
def admin_lesson_delete(request: Request, lid: int):
    require_admin(request)
    ex("DELETE FROM lessons WHERE id=?", lid)
    return go("/admin")


@app.post("/admin/lesson/{lid}/tasks")
def admin_task_create(request: Request, lid: int, title: str = Form(...)):
    require_admin(request)
    pos = q("SELECT COALESCE(MAX(pos), 0) m FROM tasks WHERE lesson_id=?", lid, one=True)["m"] + 1
    tid = ex("INSERT INTO tasks(lesson_id, pos, title) VALUES(?,?,?)", lid, pos, title)
    return go(f"/admin/task/{tid}")


@app.get("/admin/task/{tid}", response_class=HTMLResponse)
def admin_task(request: Request, tid: int):
    require_admin(request)
    task = q("SELECT * FROM tasks WHERE id=?", tid, one=True)
    if not task:
        raise HTTPException(404)
    lesson = q("SELECT * FROM lessons WHERE id=?", task["lesson_id"], one=True)
    tests = q("SELECT * FROM tests WHERE task_id=? ORDER BY is_sample DESC, pos, id", tid)
    return render(request, "admin_task.html", task=task, lesson=lesson, tests=tests)


@app.post("/admin/task/{tid}")
def admin_task_update(request: Request, tid: int, title: str = Form(...), statement: str = Form(""),
                      time_limit: float = Form(1.0), pos: int = Form(0)):
    require_admin(request)
    ex("UPDATE tasks SET title=?, statement=?, time_limit=?, pos=? WHERE id=?", title, statement,
       max(0.2, min(time_limit, 10)), pos, tid)
    return go(f"/admin/task/{tid}")


@app.post("/admin/task/{tid}/delete")
def admin_task_delete(request: Request, tid: int):
    require_admin(request)
    lid = q("SELECT lesson_id FROM tasks WHERE id=?", tid, one=True)["lesson_id"]
    ex("DELETE FROM tasks WHERE id=?", tid)
    return go(f"/admin/lesson/{lid}")


@app.post("/admin/task/{tid}/tests")
async def admin_tests_add(request: Request, tid: int, input: str = Form(""), output: str = Form(""),
                          is_sample: str = Form(""), archive: UploadFile | None = File(None), replace: str = Form("")):
    """Один тест из формы или zip: 1.in/1.out, 01/01.a, input1.txt/output1.txt ..."""
    require_admin(request)
    if replace:
        ex("DELETE FROM tests WHERE task_id=? AND is_sample=0", tid)
    pos = q("SELECT COALESCE(MAX(pos), 0) m FROM tests WHERE task_id=?", tid, one=True)["m"]
    if archive is not None and archive.filename:
        z = zipfile.ZipFile(io.BytesIO(await archive.read()))
        files = {n: z.read(n).decode("utf-8", errors="replace") for n in z.namelist() if not n.endswith("/")}
        pairs = []
        for n in files:
            base = n.rsplit("/", 1)[-1]
            for ans in (n[:-3] + ".out", n[:-3] + ".ans") if n.endswith(".in") else (n + ".a",) if re.fullmatch(r"\d+", base) else ():
                if ans in files:
                    pairs.append((n, ans))
            m = re.fullmatch(r"(.*)input(\d+)\.txt", n)
            if m and f"{m[1]}output{m[2]}.txt" in files:
                pairs.append((n, f"{m[1]}output{m[2]}.txt"))
        key = lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p[0])]
        for a, b in sorted(pairs, key=key):
            pos += 1
            ex("INSERT INTO tests(task_id, pos, input, output, is_sample) VALUES(?,?,?,?,0)", tid, pos, files[a], files[b])
    elif input.strip() or output.strip():
        norm = lambda s: s.replace("\r\n", "\n").rstrip("\n") + "\n"
        ex("INSERT INTO tests(task_id, pos, input, output, is_sample) VALUES(?,?,?,?,?)", tid, pos + 1, norm(input),
           norm(output), 1 if is_sample else 0)
    return go(f"/admin/task/{tid}")


@app.post("/admin/test/{test_id}/delete")
def admin_test_delete(request: Request, test_id: int):
    require_admin(request)
    tid = q("SELECT task_id FROM tests WHERE id=?", test_id, one=True)["task_id"]
    ex("DELETE FROM tests WHERE id=?", test_id)
    return go(f"/admin/task/{tid}")


@app.post("/admin/task/{tid}/rejudge")
def admin_task_rejudge(request: Request, tid: int):
    require_admin(request)
    ex("UPDATE submissions SET status='queued', verdict=NULL WHERE task_id=?", tid)
    return go(f"/admin/task/{tid}")


@app.post("/admin/submission/{sid}/rejudge")
def admin_sub_rejudge(request: Request, sid: int):
    require_admin(request)
    ex("UPDATE submissions SET status='queued', verdict=NULL WHERE id=?", sid)
    return go(f"/submission/{sid}")


@app.get("/admin/submissions", response_class=HTMLResponse)
def admin_submissions(request: Request, lesson: int = 0, student: int = 0, task: int = 0, group: int = 0):
    require_admin(request)
    where, args = ["1=1"], []
    if group == -1:
        where.append("s.group_id IS NULL")
    elif group:
        where.append("s.group_id=?"); args.append(group)
    if lesson:
        where.append("t.lesson_id=?"); args.append(lesson)
    if student:
        where.append("s.student_id=?"); args.append(student)
    if task:
        where.append("s.task_id=?"); args.append(task)
    subs = q("SELECT s.id, s.status, s.verdict, s.passed, s.total, s.created_at, s.code, st.name student_name, st.id student_id, "
             "t.title task_title, t.id task_id, g.name group_name, g.id group_id FROM submissions s "
             "JOIN students st ON st.id=s.student_id LEFT JOIN groups g ON g.id=s.group_id "
             f"JOIN tasks t ON t.id=s.task_id WHERE {' AND '.join(where)} ORDER BY s.id DESC LIMIT 500", *args)
    title = ""
    if student:
        r = q("SELECT name FROM students WHERE id=?", student, one=True)
        title = r["name"] if r else ""
    if task:
        r = q("SELECT title FROM tasks WHERE id=?", task, one=True)
        title = (title + " · " if title else "") + (r["title"] if r else "")
    return render(request, "admin_submissions.html", subs=subs, title=title)


@app.get("/admin/students", response_class=HTMLResponse)
def admin_students(request: Request):
    require_admin(request)
    students = q("SELECT st.*, g.name group_name, (SELECT COUNT(*) FROM submissions s WHERE s.student_id=st.id) nsubs "
                 "FROM students st LEFT JOIN groups g ON g.id=st.group_id ORDER BY st.name")
    return render(request, "admin_students.html", students=students,
                  all_study_groups=q("SELECT * FROM groups ORDER BY archived, grp, name"))


@app.post("/admin/students/{sid}")
def admin_student_update(request: Request, sid: int, name: str = Form(""), merge_into: int = Form(0), delete: str = Form("")):
    require_admin(request)
    if delete:
        ex("DELETE FROM students WHERE id=?", sid)
    elif merge_into and merge_into != sid:
        ex("UPDATE submissions SET student_id=? WHERE student_id=?", merge_into, sid)
        ex("DELETE FROM students WHERE id=?", sid)
    elif name.strip():
        key = name_key(name)
        if not q("SELECT 1 FROM students WHERE name_key=? AND id!=?", key, sid, one=True):
            ex("UPDATE students SET name=?, name_key=? WHERE id=?", " ".join(name.split()), key, sid)
    return go("/admin/students")
