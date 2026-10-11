"""Учебные группы: какая группа сейчас занимается, отчёты «ученики × уроки» по группе."""
import csv
import io

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import db

router = APIRouter()


def _q(sql, *args):
    con = db.connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _ex(sql, *args):
    con = db.connect()
    try:
        return con.execute(sql, args).lastrowid
    finally:
        con.close()


def _admin(request):
    from .main import require_admin
    require_admin(request)


def _back(request, default="/admin/groups"):
    ref = request.headers.get("referer", "")
    path = "/" + ref.split("/", 3)[3] if ref.count("/") >= 3 else default
    return RedirectResponse(path if path.startswith("/admin") else default, status_code=303)


@router.post("/admin/active-group")
def add_active_group(request: Request, group_id: int = Form(0)):
    """Группа начала занятие (одновременно могут заниматься несколько)."""
    _admin(request)
    if group_id:
        _ex("UPDATE groups SET is_active=1 WHERE id=?", group_id)
    return _back(request)


@router.post("/admin/group/{gid}/active")
def toggle_active_group(request: Request, gid: int, on: int = Form(0)):
    _admin(request)
    _ex("UPDATE groups SET is_active=? WHERE id=?", 1 if on else 0, gid)
    return _back(request)


@router.post("/admin/active-groups/clear")
def clear_active_groups(request: Request):
    _admin(request)
    _ex("UPDATE groups SET is_active=0")
    return _back(request)


@router.get("/admin/groups", response_class=HTMLResponse)
def groups_page(request: Request):
    _admin(request)
    from .main import render
    groups = _q("""SELECT g.*,
        (SELECT COUNT(DISTINCT s.student_id) FROM submissions s WHERE s.group_id=g.id) nstudents,
        (SELECT COUNT(*) FROM submissions s WHERE s.group_id=g.id) nsubs,
        (SELECT COUNT(*) FROM (SELECT DISTINCT student_id, task_id FROM submissions s WHERE s.group_id=g.id AND verdict='OK')) nsolved
        FROM groups g ORDER BY g.archived, g.grp, g.name""")
    years = [r["grp"] for r in _q("SELECT DISTINCT grp FROM lessons WHERE grp!='' ORDER BY grp")]
    return render(request, "admin_groups.html", group_rows=groups, years=years)


@router.post("/admin/groups")
def group_create(request: Request, name: str = Form(...), grp: str = Form(""), make_active: str = Form("")):
    _admin(request)
    _ex("INSERT INTO groups(name, grp, is_active) VALUES(?, ?, ?)", " ".join(name.split()), grp.strip(), 1 if make_active else 0)
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/admin/group/{gid}")
def group_update(request: Request, gid: int, name: str = Form(...), grp: str = Form(""), archived: str = Form("")):
    _admin(request)
    _ex("UPDATE groups SET name=?, grp=?, archived=?, is_active=is_active AND ? WHERE id=?",
        " ".join(name.split()), grp.strip(), 1 if archived else 0, 0 if archived else 1, gid)
    return RedirectResponse(f"/admin/group/{gid}", status_code=303)


def group_report(gid):
    """Ученики группы × уроки: сколько задач урока решено."""
    lessons = _q("""SELECT l.id, l.title, l.grp, COUNT(t.id) ntasks FROM lessons l JOIN tasks t ON t.lesson_id=l.id
                    WHERE l.id IN (SELECT DISTINCT t2.lesson_id FROM submissions s JOIN tasks t2 ON t2.id=s.task_id WHERE s.group_id=?)
                    GROUP BY l.id ORDER BY l.grp, l.id""", gid)
    students = _q("""SELECT DISTINCT st.id, st.name FROM students st JOIN submissions s ON s.student_id=st.id
                     WHERE s.group_id=? ORDER BY st.name""", gid)
    rows = _q("""SELECT s.student_id, t.lesson_id, COUNT(DISTINCT CASE WHEN s.verdict='OK' THEN s.task_id END) solved,
                        COUNT(DISTINCT s.task_id) tried
                 FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE s.group_id=? GROUP BY s.student_id, t.lesson_id""", gid)
    cell = {(r["student_id"], r["lesson_id"]): r for r in rows}
    return lessons, students, cell


@router.get("/admin/group/{gid}", response_class=HTMLResponse)
def group_page(request: Request, gid: int):
    _admin(request)
    from .main import render
    group = _q("SELECT * FROM groups WHERE id=?", gid)
    if not group:
        return RedirectResponse("/admin/groups", status_code=303)
    lessons, students, cell = group_report(gid)
    totals = {st["id"]: sum((cell.get((st["id"], l["id"])) or {"solved": 0})["solved"] for l in lessons) for st in students}
    years = [r["grp"] for r in _q("SELECT DISTINCT grp FROM lessons WHERE grp!='' ORDER BY grp")]
    return render(request, "admin_group.html", group=group[0], lessons=lessons, students=students, cell=cell,
                  totals=totals, years=years)


@router.get("/admin/group/{gid}/csv")
def group_csv(request: Request, gid: int):
    _admin(request)
    group = _q("SELECT * FROM groups WHERE id=?", gid)[0]
    lessons, students, cell = group_report(gid)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Ученик"] + [f'{l["title"]} (из {l["ntasks"]})' for l in lessons] + ["Всего решено"])
    for st in students:
        vals = [(cell.get((st["id"], l["id"])) or {"solved": 0})["solved"] for l in lessons]
        w.writerow([st["name"]] + vals + [sum(vals)])
    name = f'group-{gid}.csv'
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/admin/students/{sid}/move")
def move_student_subs(request: Request, sid: int, to_group: int = Form(0), from_group: str = Form("all")):
    """Перенести посылки ученика в другую группу (если забыли переключить группу)."""
    _admin(request)
    new = to_group or None
    if from_group == "all":
        _ex("UPDATE submissions SET group_id=? WHERE student_id=?", new, sid)
    elif from_group == "none":
        _ex("UPDATE submissions SET group_id=? WHERE student_id=? AND group_id IS NULL", new, sid)
    else:
        _ex("UPDATE submissions SET group_id=? WHERE student_id=? AND group_id=?", new, sid, int(from_group))
    _ex("UPDATE students SET group_id=? WHERE id=?", new, sid)
    return _back(request, "/admin/students")
