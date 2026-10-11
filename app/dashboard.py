"""Дашборд преподавателя: состояние сервера и успехи учеников в реальном времени."""
import asyncio
import shutil
import time

import psutil
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import db, judge

router = APIRouter()
STARTED = time.time()
_docker = {"t": 0, "ok": None, "containers": 0}


def _q(sql, *args):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _admin(request):
    if request.session.get("admin") is not True:
        raise HTTPException(403)


async def _docker_status():
    if time.time() - _docker["t"] > 5:
        _docker["t"] = time.time()
        try:
            p = await asyncio.create_subprocess_exec("docker", "ps", "-q", "--filter", "name=judge-",
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 5)
            _docker.update(ok=p.returncode == 0, containers=len(out.split()))
        except Exception:  # noqa: BLE001
            _docker.update(ok=False, containers=0)
    return _docker


@router.get("/admin/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    if request.session.get("admin") is not True:
        raise HTTPException(303, headers={"Location": "/admin/login"})
    from .main import VERDICTS, render
    lessons = _q("SELECT id, title, grp FROM lessons ORDER BY grp, id DESC")
    return render(request, "admin_dashboard.html", lessons=lessons,
                  verdicts_js={k: v for k, v in VERDICTS.items() if k})


@router.get("/admin/api/dashboard")
async def dashboard_data(request: Request, lesson: int = 0, group: int = 0):
    _admin(request)
    d = await _docker_status()
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(db.DATA)
    st = _q("""SELECT
        SUM(status='queued') queued, SUM(status='running') running,
        SUM(created_at >= datetime('now','localtime','-5 minutes')) last5,
        SUM(created_at >= datetime('now','localtime','-60 minutes')) last60,
        SUM(verdict='SE' AND created_at >= datetime('now','localtime','-60 minutes')) se60,
        COUNT(*) total FROM submissions""")[0]
    wait = _q("""SELECT AVG((julianday(judged_at) - julianday(created_at)) * 86400) avg,
                        MAX((julianday(judged_at) - julianday(created_at)) * 86400) mx
                 FROM submissions WHERE judged_at >= datetime('now','localtime','-10 minutes')""")[0]
    online = _q("SELECT COUNT(*) c FROM students WHERE last_seen >= datetime('now','localtime','-5 minutes')")[0]["c"]
    per_min = _q("""SELECT strftime('%H:%M', created_at) m, COUNT(*) c, SUM(verdict='OK') ok FROM submissions
                    WHERE created_at >= datetime('now','localtime','-30 minutes') GROUP BY m ORDER BY m""")
    server = {
        "uptime": int(time.time() - STARTED), "cpu": psutil.cpu_percent(None), "load": psutil.getloadavg()[0],
        "ncpu": psutil.cpu_count(), "mem": vm.percent, "disk_free_gb": round(disk.free / 2**30, 1),
        "docker_ok": d["ok"], "containers": d["containers"], "workers": judge.PARALLEL,
        "queued": st["queued"] or 0, "running": st["running"] or 0, "last5": st["last5"] or 0,
        "last60": st["last60"] or 0, "se60": st["se60"] or 0, "total": st["total"] or 0,
        "wait_avg": round(wait["avg"] or 0, 1), "wait_max": round(wait["mx"] or 0, 1), "online": online,
        "per_min": per_min,
    }

    if not lesson:  # по умолчанию — урок с последней активностью
        r = _q("SELECT t.lesson_id FROM submissions s JOIN tasks t ON t.id=s.task_id ORDER BY s.id DESC LIMIT 1")
        lesson = r[0]["lesson_id"] if r else (_q("SELECT id FROM lessons ORDER BY id DESC LIMIT 1") or [{"id": 0}])[0]["id"]
    tasks = _q("SELECT id, title, pos FROM tasks WHERE lesson_id=? ORDER BY pos, id", lesson)
    active = db.active_groups()
    if group == -2:  # все группы, которые занимаются сейчас
        ids = ",".join(str(a["id"]) for a in active) or "0"
        g = f" AND s.group_id IN ({ids})"
    else:
        g = "" if not group else " AND s.group_id IS NULL" if group == -1 else f" AND s.group_id={int(group)}"
    cells = _q("""SELECT s.student_id, s.task_id, COUNT(*) attempts, MAX(s.verdict='OK') solved,
                         MAX(s.passed) best_passed, MAX(s.id) last_id,
                         SUM(s.status!='done') pending, MAX(s.created_at) last_at
                  FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE t.lesson_id=?""" + g + """
                  GROUP BY s.student_id, s.task_id""", lesson)
    last_verdicts = {r["id"]: r for r in _q(
        "SELECT s.id, s.verdict, s.passed, s.total, s.status FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE t.lesson_id=?", lesson)}
    students = _q("""SELECT DISTINCT st.id, st.name, st.last_seen >= datetime('now','localtime','-5 minutes') online,
                            (SELECT name FROM groups WHERE id=st.group_id) group_name
                     FROM students st JOIN submissions s ON s.student_id=st.id JOIN tasks t ON t.id=s.task_id
                     WHERE t.lesson_id=?""" + g + """ ORDER BY st.name""", lesson)
    for c in cells:
        lv = last_verdicts.get(c["last_id"], {})
        c["last_verdict"] = lv.get("verdict") if lv.get("status") == "done" else None
        c["last_passed"], c["last_total"] = lv.get("passed"), lv.get("total")
    feed = _q("""SELECT s.id, strftime('%H:%M:%S', s.created_at) t, st.name, tk.title task, s.status, s.verdict, s.passed, s.total
                 FROM submissions s JOIN students st ON st.id=s.student_id JOIN tasks tk ON tk.id=s.task_id
                 WHERE tk.lesson_id=?""" + g.replace("s.group_id", "s.group_id") + """ ORDER BY s.id DESC LIMIT 25""", lesson)
    server["active_groups"] = [a["name"] for a in active]
    return {"server": server, "lesson": lesson, "tasks": tasks, "students": students, "cells": cells, "feed": feed,
            "now": time.strftime("%H:%M:%S")}
