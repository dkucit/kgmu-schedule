"""Скачивает расписание с сайта Казанского ГМУ и собирает JSON для сайта.

    python scraper/build.py               # скачать всё с kazangmu.ru и собрать site/data
    python scraper/build.py --offline raw # собрать из уже скачанных файлов (raw/manifest.json)

Результат: site/data/index.json + site/data/<факультет>.json
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse import parse_file, HOLIDAYS  # noqa: E402

PAGE = "https://kazangmu.ru/obuchenie/studentam/raspisanie-zanyatij"
UA = "Mozilla/5.0 (KGMU schedule mirror; +https://github.com)"
ROOT = Path(__file__).resolve().parent.parent

# Красивые короткие названия + порядок
FACULTY_META = [
    ("lech", "Лечебное дело", "Лечебный факультет", ["Лечебное дело"]),
    ("ped", "Педиатрия", "Педиатрический факультет", ["Педиатрия"]),
    ("stom", "Стоматология", "Стоматологический факультет", ["Стоматология"]),
    ("mpf", "Медико-профилактическое дело", "Медико-профилактический факультет", ["Медико-профилактическое"]),
    ("farm", "Фармация", "Фармацевтический факультет", ["Фармация"]),
    ("mbf", "Медицинская биофизика", "Медико-биологический факультет", ["биофизика"]),
    ("mbh", "Медицинская биохимия", "Медико-биологический факультет", ["биохимия"]),
    ("psy", "Психология", "Бакалавриат", ["Психология"]),
    ("vso", "Сестринское дело", "Очная форма", ["Сестринское дело (очн"]),
    ("vsoz", "Сестринское дело", "Очно-заочная форма", ["Сестринское дело (о.-заочн"]),
    ("soc", "Социальная работа", "Бакалавриат, очная", ["Социальная работа (бакалавриат, очн"]),
    ("socz", "Социальная работа", "Бакалавриат, заочная", ["Социальная работа (бакалавриат, заочн"]),
    ("socm", "Социальная работа", "Магистратура, заочная", ["Социальная работа (магистратура"]),
    ("ozm", "Общественное здравоохранение", "Магистратура, очно-заочная", ["Общественное здравоохранение (магистратура, о.-заочн"]),
    ("ozmo", "Общественное здравоохранение", "Магистратура, очная", ["Общественное здравоохранение (магистратура, очн"]),
]


def faculty_for(prog):
    for fid, name, sub, keys in FACULTY_META:
        if any(k in prog for k in keys):
            return fid
    return re.sub(r"\W+", "_", prog.lower())[:20]


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.href, self.atext, self.in_table = [], None, None, None, "", 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.in_table += 1
        elif tag == "tr" and self.in_table:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"t": "", "a": []}
        elif tag == "a" and self.cell is not None:
            self.href, self.atext = a.get("href"), ""

    def handle_endtag(self, tag):
        if tag == "a" and self.cell is not None and self.href:
            self.cell["a"].append((self.atext.strip(), self.href))
            self.href = None
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.in_table -= 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["t"] += data
        if self.href is not None:
            self.atext += data


def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), dict(r.headers)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 3 * i)
    raise last


def fetch_all(raw_dir: Path):
    raw_dir.mkdir(parents=True, exist_ok=True)
    html, _ = http_get(PAGE)
    tp = TableParser()
    tp.feed(html.decode("utf-8", "replace"))
    manifest = []
    for row in tp.rows:
        if not row or not row[0]["t"].strip() or not any(c["a"] for c in row[1:]):
            continue
        prog = re.sub(r"\s+", " ", row[0]["t"]).strip()
        for cell in row[1:]:
            for label, href in cell["a"]:
                m = re.search(r"/s/([A-Za-z0-9]+)", href)
                if not m:
                    continue
                url = href.split("?")[0].rstrip("/") + "/download"
                data, hdr = http_get(url)
                cd = hdr.get("Content-Disposition", "")
                fn = (re.search(r'filename="([^"]+)"', cd) or [None, m.group(1) + ".bin"])[1]
                (raw_dir / fn).write_bytes(data)
                manifest.append({"prog": prog, "label": re.sub(r"\s+", " ", label).strip(), "tok": m.group(1),
                                 "fn": fn, "lm": hdr.get("Last-Modified"), "size": len(data)})
                print(f"  ✓ {prog} / {label}: {fn} ({len(data)//1024} KB)")
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    return manifest


def semester_start(raw_dir: Path, manifest):
    """Понедельник недели №1: осень — неделя с 1 сентября, весна — 2-я неделя февраля."""
    import openpyxl
    for m in manifest:
        if m["fn"].endswith(".xlsx"):
            ws = openpyxl.load_workbook(raw_dir / m["fn"], read_only=True).worksheets[0]
            for row in ws.iter_rows(min_row=1, max_row=4, values_only=True):
                for v in row:
                    mm = re.search(r"(осенний|весенний) семестр (\d{4})/(\d{4})", str(v or ""))
                    if mm:
                        if mm.group(1) == "осенний":
                            d = dt.date(int(mm.group(2)), 9, 1)
                        else:
                            d = dt.date(int(mm.group(3)), 2, 9)
                        return (d - dt.timedelta(days=d.weekday())).isoformat(), mm.group(0)
    d = dt.date.today()
    return (d - dt.timedelta(days=d.weekday())).isoformat(), ""


def compact(ev):
    ev = {k: v for k, v in ev.items() if k != "raw"}
    return ev


class BuildRejected(Exception):
    pass


def write_status(out_dir: Path, ok: bool, message: str, problems=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    st = {"ok": ok, "message": message, "problems": problems or [],
          "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    (out_dir / "status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    print(("✓ " if ok else "✗ ") + message)


def build(raw_dir: Path, out_dir: Path, manifest):
    out_dir.mkdir(parents=True, exist_ok=True)
    problems = []
    start, sem_label = semester_start(raw_dir, manifest)
    facs = {}
    for m in manifest:
        fid = faculty_for(m["prog"])
        meta = next((x for x in FACULTY_META if x[0] == fid), (fid, m["prog"], "", []))
        fac = facs.setdefault(fid, {"id": fid, "name": meta[1], "sub": meta[2], "prog": m["prog"],
                                    "groups": {}, "pdfs": [], "sources": []})
        src_url = f"https://cloud.kazangmu.ru/index.php/s/{m['tok']}"
        fac["sources"].append({"label": m["label"], "url": src_url, "lm": m.get("lm"), "fn": m["fn"]})
        if m["fn"].lower().endswith(".pdf"):
            cm = re.match(r"(\d)", m["label"])
            fac["pdfs"].append({"label": m["label"], "course": int(cm.group(1)) if cm else None, "url": src_url})
            continue
        try:
            _, res, kind = parse_file(raw_dir / m["fn"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! не удалось разобрать {m['fn']}: {e}")
            problems.append(f"{m['prog']} / {m['label']}: файл {m['fn']} не разобран ({e})")
            continue
        if not res:
            problems.append(f"{m['prog']} / {m['label']}: в файле {m['fn']} не найдено ни одной группы")
        for g, v in res.items():
            slot = fac["groups"].setdefault(g, {"course": v["course"], "ev": [], "src": []})
            slot["course"] = slot["course"] or v["course"]
            slot["ev"].extend(compact(e) for e in v["events"])
            if src_url not in slot["src"]:
                slot["src"].append(src_url)
    # дедупликация и сортировка
    for fac in facs.values():
        for g, v in fac["groups"].items():
            seen, uniq = set(), []
            for e in v["ev"]:
                key = json.dumps(e, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    uniq.append(e)
            uniq.sort(key=lambda e: (e.get("date", ""), e.get("d", 9), e.get("s", "")))
            v["ev"] = uniq
    # защита от поломки: если групп стало намного меньше — не перезаписываем данные
    new_count = sum(len(f["groups"]) for f in facs.values())
    old_index = out_dir / "index.json"
    if old_index.exists():
        old = json.loads(old_index.read_text("utf-8"))
        old_count = sum(len(g) for f in old["faculties"] for g in f["courses"].values())
        if old_count and new_count < old_count * 0.7:
            raise BuildRejected(f"Групп после разбора стало {new_count} вместо {old_count}. "
                                f"Похоже, вуз поменял формат таблиц — оставляю прошлые данные.", problems)
    if new_count == 0:
        raise BuildRejected("Не удалось разобрать ни одной группы — оставляю прошлые данные.", problems)
    order = [x[0] for x in FACULTY_META]
    index = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "semester": sem_label, "weekStart": start, "source": PAGE, "holidays": sorted(HOLIDAYS), "faculties": []}
    for fid in sorted(facs, key=lambda f: order.index(f) if f in order else 99):
        fac = facs[fid]
        body = json.dumps({"id": fid, "groups": fac["groups"]}, ensure_ascii=False, separators=(",", ":"))
        (out_dir / f"{fid}.json").write_text(body, "utf-8")
        courses = {}
        for g, v in fac["groups"].items():
            courses.setdefault(str(v["course"] or "?"), []).append(g)
        for c in courses.values():
            c.sort(key=lambda s: (len(re.sub(r"\D", "", s)), s))
        index["faculties"].append({"id": fid, "name": fac["name"], "sub": fac["sub"], "prog": fac["prog"],
                                   "courses": dict(sorted(courses.items())), "pdfs": fac["pdfs"],
                                   "sources": fac["sources"],
                                   "hash": hashlib.md5(body.encode()).hexdigest()[:10]})
        print(f"  {fid:5} {fac['name']}: {len(fac['groups'])} групп, {len(body)//1024} KB")
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
    write_status(out_dir, not problems, f"Обновлено: {new_count} групп" + (f", проблем: {len(problems)}" if problems else ""), problems)
    return index


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", metavar="RAW_DIR", help="не скачивать, взять файлы из папки")
    ap.add_argument("--out", default=str(ROOT / "site" / "data"))
    a = ap.parse_args()
    out = Path(a.out)
    try:
        if a.offline:
            raw = Path(a.offline)
            man = json.loads((raw / "manifest.json").read_text("utf-8"))
        else:
            raw = ROOT / "raw"
            print("Скачиваю расписание с", PAGE)
            man = fetch_all(raw)
            if not man:
                raise BuildRejected("На странице расписания КГМУ не нашлось ни одной ссылки на файлы — возможно, страницу переделали.", [])
    except BuildRejected as e:
        write_status(out, False, e.args[0], e.args[1] if len(e.args) > 1 else [])
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        write_status(out, False, f"Не удалось скачать расписание с сайта КГМУ: {e}")
        sys.exit(1)
    try:
        build(raw, out, man)
    except BuildRejected as e:
        write_status(out, False, e.args[0], e.args[1])
        sys.exit(1)
