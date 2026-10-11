"""Проверка посылок: компиляция и запуск в docker-контейнере без сети, с лимитами."""
import asyncio
import json
import shutil
import uuid

from . import db

IMAGE = "mshp-cpp"
PARALLEL = 8          # одновременных контейнеров
MEMORY = "256m"
MAX_SHOW = 3000       # сколько символов ввода/вывода показывать ученику
RUNS = db.DATA / "runs"


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def outputs_match(expected: str, got: str) -> bool:
    """Сравнение по токенам; числа с плавающей точкой — с точностью 1e-6."""
    a, b = expected.split(), got.split()
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        fx, fy = _num(x), _num(y)
        if fx is None or fy is None or "." not in x + y:
            return False
        if abs(fx - fy) > 1e-6 * max(1.0, abs(fx)):
            return False
    return True


def _cut(s: str) -> str:
    return s if len(s) <= MAX_SHOW else s[:MAX_SHOW] + "\n…(обрезано)"


async def judge(sub_id: int):
    con = db.connect()
    sub = con.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    task = con.execute("SELECT * FROM tasks WHERE id=?", (sub["task_id"],)).fetchone()
    tests = con.execute("SELECT * FROM tests WHERE task_id=? ORDER BY is_sample DESC, pos, id", (task["id"],)).fetchall()
    tl = task["time_limit"]

    box = RUNS / f"{sub_id}-{uuid.uuid4().hex[:6]}"
    (box / "tests").mkdir(parents=True)
    (box / "out").mkdir()
    for p in (box, box / "tests", box / "out"):
        p.chmod(0o777)
    (box / "main.cpp").write_text(sub["code"])
    for i, t in enumerate(tests):
        (box / "tests" / f"{i:03d}.in").write_text(t["input"])

    name = f"judge-{box.name}"
    cmd = [
        "docker", "run", "--rm", "--init", "--name", name,
        "--network", "none", "--memory", MEMORY, "--memory-swap", MEMORY,
        "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=64m", "--ulimit", "fsize=33554432",
        "--user", "65534:65534", "-e", f"TL={tl}", "-e", f"TL_MS={int(tl * 1000)}",
        "-v", f"{box}:/box", IMAGE, "/run.sh",
    ]
    verdict, details, compile_log, passed = "SE", [], "", 0
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60 + len(tests) * (tl + 1))
        except asyncio.TimeoutError:
            await (await asyncio.create_subprocess_exec("docker", "kill", name)).wait()
            raise RuntimeError("judge timeout")
        o = box / "out"
        compile_log = (o / "compile.txt").read_text(errors="replace") if (o / "compile.txt").exists() else out.decode(errors="replace")
        if not (o / "compile.rc").exists():
            raise RuntimeError("sandbox failed: " + out.decode(errors="replace")[:2000])
        if (o / "compile.rc").read_text().strip() != "0":
            verdict = "CE"
        else:
            verdict = "OK"
            for i, t in enumerate(tests):
                rcf = o / f"{i:03d}.rc"
                if not rcf.exists():
                    details.append({"n": i + 1, "verdict": "SK", "ms": 0, "rc": 0, "sample": bool(t["is_sample"]),
                                    "input": _cut(t["input"]), "expected": _cut(t["output"]), "got": "", "stderr": ""})
                    continue
                rc, ms = (rcf.read_text().split() + ["0"])[:2] if rcf.exists() else ("137", "0")
                rc, ms = int(rc), int(ms)
                got = (o / f"{i:03d}.out").read_text(errors="replace") if (o / f"{i:03d}.out").exists() else ""
                err = (o / f"{i:03d}.err").read_text(errors="replace") if (o / f"{i:03d}.err").exists() else ""
                if rc in (124, 137) and ms >= tl * 1000 * 0.9:
                    v = "TLE"
                elif rc == 137:
                    v = "ML"
                elif rc != 0:
                    v = "RE"
                elif outputs_match(t["output"], got):
                    v = "OK"
                else:
                    v = "WA"
                if v == "OK":
                    passed += 1
                elif verdict == "OK":
                    verdict = v
                d = {"n": i + 1, "verdict": v, "ms": ms, "rc": rc, "sample": bool(t["is_sample"]),
                     "input": _cut(t["input"]), "expected": _cut(t["output"]), "got": _cut(got), "stderr": _cut(err)}
                details.append(d)
    except Exception as e:  # noqa: BLE001
        verdict, compile_log = "SE", f"Ошибка системы проверки: {e}"
    finally:
        shutil.rmtree(box, ignore_errors=True)
    con.execute(
        "UPDATE submissions SET status='done', judged_at=datetime('now', 'localtime'), verdict=?, passed=?, total=?, compile_log=?, details=? WHERE id=?",
        (verdict, passed, len(tests), compile_log[:20000], json.dumps(details, ensure_ascii=False), sub_id),
    )
    con.close()


async def worker_loop():
    con = db.connect()
    con.execute("UPDATE submissions SET status='queued' WHERE status='running'")
    sem = asyncio.Semaphore(PARALLEL)
    running = set()

    async def run(sid):
        async with sem:
            try:
                await judge(sid)
            finally:
                running.discard(sid)

    while True:
        free = PARALLEL - len(running)
        if free > 0:
            rows = con.execute("SELECT id FROM submissions WHERE status='queued' ORDER BY id LIMIT ?", (free,)).fetchall()
            for r in rows:
                con.execute("UPDATE submissions SET status='running' WHERE id=?", (r["id"],))
                running.add(r["id"])
                asyncio.create_task(run(r["id"]))
        await asyncio.sleep(0.3)
