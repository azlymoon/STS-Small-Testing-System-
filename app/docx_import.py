"""Разбор docx с задачами МШП: заголовки задач, разделы условия, код, таблицы примеров."""
import re
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SECTIONS = ["Условие", "Формат входных данных", "Формат выходных данных", "Примечания", "Примеры"]


def _runs_text(p):
    out = []
    for r in p.iter():
        if r.tag == W + "t":
            out.append(r.text or "")
        elif r.tag in (W + "br", W + "cr"):
            out.append("\n")
        elif r.tag == W + "tab":
            out.append("\t")
    return "".join(out)


def _md_inline(p):
    """Текст абзаца с простым markdown: степени, жирный."""
    parts = []
    for r in p.findall(W + "r"):
        t = "".join((e.text or "") if e.tag == W + "t" else "\n" for e in r if e.tag in (W + "t", W + "br"))
        if not t:
            continue
        rpr = r.find(W + "rPr")
        sup = rpr is not None and rpr.find(W + "vertAlign") is not None and rpr.find(W + "vertAlign").get(W + "val") == "superscript"
        bold = rpr is not None and rpr.find(W + "b") is not None and rpr.find(W + "b").get(W + "val") not in ("0", "false")
        t = t.replace("<", "&lt;").replace(">", "&gt;")
        if sup:
            t = f"<sup>{t}</sup>"
        elif bold and t.strip():
            t = f"**{t.strip()}** " if t.endswith(" ") else f"**{t.strip()}**"
        parts.append(t)
    return "".join(parts).replace("****", "")


def _list_level(p):
    ppr = p.find(W + "pPr")
    if ppr is None:
        return None
    num = ppr.find(W + "numPr")
    if num is None:
        return None
    il = num.find(W + "ilvl")
    return int(il.get(W + "val")) if il is not None else 0


def _is_code(p):
    ppr = p.find(W + "pPr")
    return ppr is not None and ppr.find(W + "shd") is None and ppr.find(W + "ind") is not None


def parse_docx(path_or_file):
    with zipfile.ZipFile(path_or_file) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    tasks, cur, section = [], None, None
    code_buf = []

    def flush_code():
        nonlocal code_buf
        if cur is not None and code_buf:
            cur["sections"].setdefault(section or "Условие", []).append("```cpp\n" + "\n".join(code_buf).rstrip() + "\n```")
        code_buf = []

    for el in body:
        if el.tag == W + "p":
            text = _runs_text(el).strip()
            lvl = _list_level(el)
            if lvl == 1 and text:
                flush_code()
                cur = {"title": re.sub(r'(["“«])_', r"\1", text).lstrip("_"), "sections": {}, "examples": []}
                tasks.append(cur)
                section = None
                continue
            if cur is None:
                continue
            if text in SECTIONS:
                flush_code()
                section = text
                continue
            if (_is_code(el) or (code_buf and text in ("}", "};"))) and section in ("Примечания", "Условие", None):
                code_buf.append(_runs_text(el))
                continue
            flush_code()
            if re.fullmatch(r"Простая\s*-\s*Средняя\s*-\s*Сложная\s*-\s*Олимпиадная", " ".join(text.split())):
                lvl_name = next((r.findtext(W + "t") for r in el.findall(W + "r")
                                 if r.find(W + "rPr/" + W + "b") is not None and (r.findtext(W + "t") or "").strip()), "")
                if lvl_name.strip():
                    cur["sections"].setdefault("Условие", []).append(f"*Сложность: {lvl_name.strip().lower()}*")
                continue
            if text:
                cur["sections"].setdefault(section or "Условие", []).append(_md_inline(el).strip())
        elif el.tag == W + "tbl" and cur is not None:
            flush_code()
            rows = el.findall(W + "tr")
            for row in rows:
                cells = row.findall(W + "tc")
                vals = ["\n".join(_runs_text(p) for p in c.findall(W + "p")).strip("\n") for c in cells]
                if len(vals) >= 2 and "ВХОДНЫЕ" not in vals[0].upper():
                    cur["examples"].append((vals[0].strip() + "\n", vals[1].strip() + "\n"))
    flush_code()

    result = []
    for t in tasks:
        if not t["examples"] and not any(t["sections"].values()):
            continue  # блок только с картинками-теорией
        md = []
        for s in SECTIONS[:-1]:
            if t["sections"].get(s):
                if s != "Условие":
                    md.append(f"### {s}")
                md.append("\n\n".join(t["sections"][s]))
        result.append({"title": t["title"], "statement": "\n\n".join(md), "examples": t["examples"]})
    return result


if __name__ == "__main__":
    import sys, json
    for t in parse_docx(sys.argv[1]):
        print("=" * 60, t["title"])
        print(t["statement"])
        print("EXAMPLES:", json.dumps(t["examples"], ensure_ascii=False))
