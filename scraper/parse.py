"""Парсер расписания Казанского ГМУ (xlsx с cloud.kazangmu.ru) -> JSON для сайта.

Форматы:
  * grid   — курс целиком: строки = 45-мин. слоты по дням, столбцы = группы,
             в тексте недели «(2-9,11-17)»; объединённые ячейки = длительность/поток.
  * weekly — то же, но по листу на неделю («1 неделя», «2 неделя» …), без номеров недель.
  * cycle  — циклы старших курсов: таблица «Группа | Дисциплина | … | Даты | Начало | Окончание».
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import openpyxl

DAYS = {"ПН": 0, "ВТ": 1, "СР": 2, "ЧТ": 3, "ПТ": 4, "СБ": 5, "ВС": 6}
GROUP_RE = re.compile(r"^[А-ЯA-Z]?\d{3,5}[а-яa-zА-ЯA-Z]*(-\d+)?$")
# Нерабочие праздничные дни (циклы по ним не идут)
HOLIDAYS = {"2026-11-04", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03", "2027-01-04",
            "2027-01-05", "2027-01-06", "2027-01-07", "2027-01-08", "2027-02-23", "2027-03-08",
            "2027-05-01", "2027-05-09", "2027-06-12"}


def fmt_time(v):
    if v is None:
        return None
    if isinstance(v, dt.time):
        return f"{v.hour:02d}:{v.minute:02d}"
    if isinstance(v, dt.datetime):
        return f"{v.hour:02d}:{v.minute:02d}"
    if isinstance(v, (int, float)):
        m = round(float(v) * 24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"
    s = str(v).strip()
    m = re.match(r"^(\d{1,2})[:.](\d{2})", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def clean(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def parse_weeks(s):
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def split_parens(s):
    """Возвращает список (start, end, inner) для скобок верхнего уровня."""
    res, depth, start = [], 0, None
    for i, ch in enumerate(s):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                res.append((start, i, s[start + 1:i]))
    return res


def parse_entry(text, has_weeks=True):
    """'Лекция: Химия (2-16) Иванов И.И. (НУК, ауд. 3)' -> dict."""
    t = clean(text)
    kind = "pr"
    m = re.match(r"^Лекция\s*[:.]?\s*", t, re.I)
    if m:
        kind = "lec"
        t = t[m.end():]
    if re.search(r"физическ\w* культур|элективн\w* дисциплин\w* по физ", t, re.I):
        kind = "pe"
    ev = {"k": kind, "raw": clean(text)}
    parens = split_parens(t)
    weeks_p = next((p for p in parens if re.fullmatch(r"[\d,\-\s]+", p[2]) and re.search(r"\d", p[2])), None)
    if not weeks_p:  # несбалансированные скобки в названии
        mw = re.search(r"\(([\d,\s\-]*\d[\d,\s\-]*)\)", t)
        if mw:
            weeks_p = (mw.start(), mw.end() - 1, mw.group(1))
            parens = [p for p in parens if p[0] > weeks_p[1]] or parens
    place_p = parens[-1] if parens and parens[-1] is not weeks_p and parens[-1][1] >= len(t) - 2 else None
    if weeks_p:
        ev["w"] = parse_weeks(weeks_p[2])
        title = t[:weeks_p[0]]
        rest_start = weeks_p[1] + 1
    elif place_p:
        title = t[:place_p[0]]
        rest_start = place_p[0]
    else:
        title, rest_start = t, len(t)
    rest_end = place_p[0] if place_p and place_p[0] >= rest_start else len(t)
    teacher = t[rest_start:rest_end]
    # «+2 нед» и подобные пометки
    extra = re.match(r"^\s*(\+\s*\d+\s*нед\.?)", teacher)
    if extra:
        ev["note"] = extra.group(1)
        teacher = teacher[extra.end():]
    title = clean(title)
    # weekly-формат: «Тема – Преподаватель (место)»
    if not weeks_p and " – " in title:
        title, teacher = title.rsplit(" – ", 1)
    sub = re.search(r"\b(\d)\s*подгр\.?", title)
    if sub:
        ev["sg"] = int(sub.group(1))
        title = clean(title[:sub.start()] + title[sub.end():])
    ev["t"] = title.strip(" ;,-–") or clean(text)
    teacher = clean(teacher).strip(" ,;–-")
    if teacher and not re.fullmatch(r"(Не назначено[, ]*)+", teacher):
        ev["p"] = teacher
    if place_p:
        place = clean(place_p[2])
        if not re.fullmatch(r"(Не назначено[, ]*)+", place):
            ev["l"] = place
    return ev


def split_multi(text):
    parts = re.split(r"\s*/\s*\n\s*|\s+/\s+", str(text).strip())
    return [p for p in parts if p.strip()]


def merged_map(ws):
    m = {}
    for rng in ws.merged_cells.ranges:
        m[(rng.min_row, rng.min_col)] = (rng.max_row, rng.max_col)
    return m


def meta_from_sheet(ws):
    meta = {}
    for row in ws.iter_rows(min_row=1, max_row=12, max_col=6):
        vals = [c.value for c in row]
        for i, v in enumerate(vals):
            if not isinstance(v, str):
                continue
            nxt = next((x for x in vals[i + 1:] if x not in (None, "")), None)
            if v.startswith("Направление подготовки"):
                meta["program"] = clean(nxt if nxt else v.split(":", 1)[-1])
            elif v.startswith("Уровень образования"):
                meta["level"] = clean(nxt if nxt else v.split(":", 1)[-1])
            elif v.startswith("Форма обучения"):
                meta["form"] = clean(nxt if nxt else v.split(":", 1)[-1])
    return meta


def parse_grid_sheet(ws, week=None):
    """Возвращает {group: [events]} для листа-сетки."""
    merges = merged_map(ws)
    max_row, max_col = ws.max_row, ws.max_column
    grid = {}
    for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        for c in row:
            if c.value not in (None, ""):
                grid[(c.row, c.column)] = c.value
    day_rows = [r for r in range(1, max_row + 1)
                if isinstance(grid.get((r, 1)), str) and grid[(r, 1)].strip().upper() in DAYS]
    if not day_rows:
        return {}
    first = day_rows[0]
    # строка с номерами групп — ближайшая сверху, где в D.. группы
    groups = {}
    for r in range(first - 1, 0, -1):
        cand = {col: clean(grid[(r, col)]) for col in range(4, max_col + 1)
                if (r, col) in grid and GROUP_RE.match(clean(grid[(r, col)]))}
        if cand:
            groups = cand
            break
    if not groups:
        return {}
    times = {}
    for r in day_rows:
        times[r] = (fmt_time(grid.get((r, 2))), fmt_time(grid.get((r, 3))))
    out = {g: [] for g in groups.values()}
    day_rows_set = set(day_rows)
    for r in day_rows:
        day = DAYS[grid[(r, 1)].strip().upper()]
        for col in range(4, max_col + 1):
            v = grid.get((r, col))
            if v in (None, "") or not isinstance(v, str):
                continue
            r2, c2 = merges.get((r, col), (r, col))
            end_row = max([x for x in range(r, r2 + 1) if x in day_rows_set] or [r])
            s, e = times[r][0], times.get(end_row, times[r])[1]
            if not s or not e:
                continue
            targets = [groups[cc] for cc in range(col, c2 + 1) if cc in groups]
            for part in split_multi(v):
                ev = parse_entry(part)
                ev.update({"d": day, "s": s, "e": e})
                if week is not None:
                    ev["w"] = [week]
                for g in targets:
                    out[g].append(dict(ev))
    return out


def daterange_weekdays(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            yield d
        d += dt.timedelta(days=1)


def to_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", str(v).strip())
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def parse_cycle_sheet(ws):
    hdr = None
    for row in ws.iter_rows(min_row=1, max_row=30):
        names = [clean(c.value) if c.value else "" for c in row]
        if "Группа" in names and "Дисциплина" in names:
            hdr = {n: i for i, n in enumerate(names) if n}
            hdr_row = row[0].row
            break
    if not hdr:
        return {}
    col = lambda name: next((i for n, i in hdr.items() if n.startswith(name)), None)
    cg, cd, cp, cl, cr, cdt, cs, ce = (col("Группа"), col("Дисциплина"), col("Преподаватель"), col("Локация"),
                                       col("Помещение"), col("Даты"), col("Начало"), col("Окончание"))
    out = {}
    cur = {"g": None, "t": None, "p": None, "l": None, "r": None}
    for row in ws.iter_rows(min_row=hdr_row + 1):
        v = [c.value for c in row]
        get = lambda i: v[i] if i is not None and i < len(v) else None
        if get(cg):
            g = clean(get(cg))
            if GROUP_RE.match(g):
                cur = {"g": g, "t": None, "p": None, "l": None, "r": None}
            else:
                continue
        if get(cd):
            cur.update(t=clean(get(cd)), p=clean(get(cp)) if get(cp) else None,
                       l=clean(get(cl)) if get(cl) else None, r=clean(get(cr)) if get(cr) else None)
        dates = get(cdt)
        if not cur["g"] or not cur["t"] or dates in (None, ""):
            continue
        s, e = fmt_time(get(cs)), fmt_time(get(ce))
        if isinstance(dates, str) and re.search(r"[–-]", dates) and re.search(r"\d{2}\.\d{2}\.\d{4}.*\d{2}\.\d{2}\.\d{4}", dates):
            a, b = re.findall(r"\d{2}\.\d{2}\.\d{4}", dates)[:2]
            days = list(daterange_weekdays(to_date(a), to_date(b)))
        else:
            d1 = to_date(dates)
            days = [d1] if d1 else []
        place = ", ".join(x for x in (cur["l"], cur["r"]) if x)
        for d in days:
            ev = {"date": d.isoformat(), "s": s, "e": e, "t": cur["t"], "k": "cyc"}
            if cur["p"]:
                ev["p"] = cur["p"]
            if place:
                ev["l"] = place
            out.setdefault(cur["g"], []).append(ev)
    return out


def parse_file(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    first = wb[wb.sheetnames[0]]
    meta = meta_from_sheet(first)
    is_cycle = "ЦИКЛ" in str(first["B2"].value or "").upper()
    result = {}
    for name in wb.sheetnames:
        ws = wb[name]
        if is_cycle:
            part = parse_cycle_sheet(ws)
        else:
            m = re.match(r"^\s*(\d+)\s*недел", name)
            part = parse_grid_sheet(ws, week=int(m.group(1)) if m else None)
        course = None
        mc = re.match(r"^\s*(\d)\s*курс", name)
        if mc:
            course = int(mc.group(1))
        else:
            b9 = ws["B9"].value
            if str(b9).strip().isdigit():
                course = int(str(b9).strip())
        for g, evs in part.items():
            slot = result.setdefault(g, {"course": course, "events": []})
            slot["events"].extend(evs)
    return meta, result, ("cycle" if is_cycle else "grid")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        meta, res, kind = parse_file(p)
        print(Path(p).name, kind, meta, len(res), sum(len(v["events"]) for v in res.values()))
