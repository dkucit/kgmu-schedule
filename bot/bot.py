"""Пары КГМУ — Telegram-бот с напоминаниями о парах.

Запуск:  BOT_TOKEN=123:ABC ADMIN_IDS=111111 python bot.py
Работает через long polling: не нужны домен, HTTPS и открытые порты.
Данные расписания берёт с сайта (GitHub Pages), подписчиков хранит в SQLite.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
from html import escape
from urllib.parse import urlencode

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SITE = os.environ.get("SITE_URL", "https://dkucit.github.io/kgmu-schedule/").rstrip("/") + "/"
DB_PATH = os.environ.get("DB_PATH", "kgmu_bot.sqlite3")
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS", ""))}
TZ = dt.timezone(dt.timedelta(hours=3))  # Казань
DEFAULT_OFF = 120   # минут до первой пары
EVENING = -1        # режим «вечером накануне»
EVENING_AT = 20 * 60
DATA_TTL = 30 * 60
CHANGE_DAYS = 7     # за сколько дней вперёд следить за изменениями

log = logging.getLogger("kgmu")
DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DOW_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MON = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
KIND = {"lec": "Лекция", "pr": "Практика", "cyc": "Цикл", "pe": "Физкультура"}
TR = {"и": "i", "П": "P", "м": "m", "н": "n", "а": "a", "М": "M", "Н": "N"}


def slug(g: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", "".join(TR.get(c, c) for c in str(g)))


def mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def now_kzn() -> dt.datetime:
    return dt.datetime.now(TZ)


def human_date(d: dt.date) -> str:
    return f"{DOW[d.weekday()]}, {d.day} {MON[d.month - 1]}"


def off_text(off: int) -> str:
    return "вечером накануне в 20:00" if off == EVENING else f"за {off / 60:g} ч до первой пары".replace(".", ",")


def sg_text(sg) -> str:
    return f"{sg}-я подгруппа" if sg else "все подгруппы"


# ---------- данные расписания ----------
class Schedule:
    def __init__(self):
        self.index = None
        self.facs: dict = {}
        self.status = None
        self.loaded_at = 0.0
        self.version = 0  # растёт, когда пришли новые данные
        self.lock = asyncio.Lock()

    async def refresh(self, force=False):
        async with self.lock:
            if not force and self.index and time.time() - self.loaded_at < DATA_TTL:
                return
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(SITE + "data/index.json", params={"t": int(time.time())}) as r:
                    r.raise_for_status()
                    index = await r.json(content_type=None)
                try:
                    async with s.get(SITE + "data/status.json", params={"t": int(time.time())}) as r:
                        self.status = await r.json(content_type=None) if r.status == 200 else None
                except Exception:  # noqa: BLE001
                    self.status = None
                facs, changed = {}, False
                old_hash = (self.index or {}).get("_hash", {})
                for f in index["faculties"]:
                    if not f["courses"]:
                        continue
                    if old_hash.get(f["id"]) == f.get("hash") and f["id"] in self.facs:
                        facs[f["id"]] = self.facs[f["id"]]
                        continue
                    async with s.get(f"{SITE}data/{f['id']}.json", params={"v": f.get("hash", "")}) as r:
                        r.raise_for_status()
                        facs[f["id"]] = (await r.json(content_type=None))["groups"]
                    changed = True
            index["_hash"] = {f["id"]: f.get("hash") for f in index["faculties"]}
            self.index, self.facs, self.loaded_at = index, facs, time.time()
            if changed:
                self.version += 1
                log.info("Расписание загружено: %s групп, обновлено %s",
                         sum(len(v) for v in facs.values()), index.get("updated"))

    def find_group(self, text: str):
        q = re.sub(r"^g-", "", re.sub(r"\s+", "", str(text)).lower())
        if not q or not self.index:
            return None
        for f in self.index["faculties"]:
            for gs in f["courses"].values():
                for g in gs:
                    if q in (g.lower(), slug(g).lower()):
                        return f["id"], g
        return None

    def fac_name(self, fid):
        f = next((x for x in (self.index or {}).get("faculties", []) if x["id"] == fid), None)
        return f["name"] if f else fid

    def course_of(self, fid, g):
        f = next((x for x in (self.index or {}).get("faculties", []) if x["id"] == fid), None)
        for c, gs in (f or {}).get("courses", {}).items():
            if g in gs:
                return c
        return "?"

    def has_subgroups(self, fac, g):
        grp = self.facs.get(fac, {}).get(g)
        return bool(grp and any(e.get("sg") for e in grp["ev"]))

    def week_no(self, d: dt.date) -> int:
        start = dt.date.fromisoformat(self.index["weekStart"])
        monday = d - dt.timedelta(days=d.weekday())
        return (monday - start).days // 7 + 1

    def day(self, fac: str, g: str, d: dt.date, sg: int = 0):
        if not self.index or d.isoformat() in self.index.get("holidays", []):
            return []
        grp = self.facs.get(fac, {}).get(g)
        if not grp:
            return []
        iso, wd, w = d.isoformat(), d.weekday(), self.week_no(d)
        evs = [e for e in grp["ev"]
               if (e["date"] == iso if "date" in e else e.get("d") == wd and (not e.get("w") or w in e["w"]))
               and not (sg and e.get("sg") and e["sg"] != sg)]
        return sorted(evs, key=lambda e: (mins(e["s"]), mins(e["e"])))


SCHED = Schedule()


def format_day(evs) -> str:
    parts = []
    for e in evs:
        tags = [KIND.get(e.get("k"), "Занятие")] + ([f"{e['sg']} подгр."] if e.get("sg") else [])
        s = f"<b>{e['s']}–{e['e']}</b> · {' · '.join(tags)}\n📚 {escape(e['t'])}"
        if e.get("l"):
            s += f"\n📍 {escape(e['l'])}"
        if e.get("p"):
            s += f"\n👤 {escape(e['p'])}"
        parts.append(s)
    return "\n\n".join(parts)


def short_line(e) -> str:
    place = f" ({escape(e['l'])})" if e.get("l") else ""
    return f"{e['s']}–{e['e']} {escape(e['t'])}{place}"


# ---------- база ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  chat_id INTEGER PRIMARY KEY, name TEXT, fac TEXT, grp TEXT,
  off INTEGER DEFAULT 120, active INTEGER DEFAULT 1, sent TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS snap(grp TEXT, sg INTEGER, date TEXT, fp TEXT, PRIMARY KEY(grp, sg, date));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS stats(date TEXT, kind TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(date, kind));
""")
cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
if "sg" not in cols:
    db.execute("ALTER TABLE users ADD COLUMN sg INTEGER DEFAULT 0")
if "changes" not in cols:
    db.execute("ALTER TABLE users ADD COLUMN changes INTEGER DEFAULT 1")
db.commit()


def get_user(chat_id):
    return db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def upsert(chat_id, **kw):
    db.execute("INSERT OR IGNORE INTO users(chat_id, off) VALUES(?, ?)", (chat_id, DEFAULT_OFF))
    if kw:
        db.execute(f"UPDATE users SET {', '.join(k + '=?' for k in kw)} WHERE chat_id=?", (*kw.values(), chat_id))
    db.commit()
    return get_user(chat_id)


def kv_get(k, default=None):
    r = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    db.commit()


def stat_inc(kind, n=1):
    d = now_kzn().date().isoformat()
    db.execute("INSERT INTO stats(date, kind, n) VALUES(?, ?, ?) ON CONFLICT(date, kind) DO UPDATE SET n=n+excluded.n",
               (d, kind, n))
    db.commit()


# ---------- кнопки ----------
BOT_USERNAME = ""


def app_url(g=None) -> str:
    q = {"bot": BOT_USERNAME}
    if g:
        q["g"] = slug(g)
    return SITE + "?" + urlencode(q)


def open_kb(g=None, text="📅 Открыть расписание"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=app_url(g)))]])


def time_kb(cur):
    opts = [(60, "1 ч"), (90, "1,5 ч"), (120, "2 ч"), (180, "3 ч")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if m == cur else "") + t, callback_data=f"off:{m}") for m, t in opts],
        [InlineKeyboardButton(text=("✅ " if cur == EVENING else "") + "🌙 Вечером накануне (20:00)", callback_data=f"off:{EVENING}")],
    ])


def sg_kb(cur):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("✅ " if cur == v else "") + t, callback_data=f"sg:{v}")
        for v, t in ((0, "Все"), (1, "1-я"), (2, "2-я"))
    ]])


async def set_menu(bot: Bot, chat_id, g):
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_url(g))))
    except Exception as e:  # noqa: BLE001
        log.warning("menu button: %s", e)


HELP = ("Я присылаю расписание <b>Казанского ГМУ</b>.\n\n"
        "• Пришлите номер группы (например <b>1201</b>), чтобы выбрать или сменить её\n"
        "/today — пары на сегодня\n/tomorrow — пары на завтра\n"
        "/time — когда присылать напоминание\n/subgroup — ваша подгруппа\n"
        "/changes — уведомления об изменениях в расписании\n"
        "/stop — выключить напоминания\n/start — включить снова\n\n"
        "Если учебный отдел поменяет ваши пары на ближайшую неделю — я сразу напишу.\n"
        "Кнопка «Расписание» внизу открывает расписание прямо в Telegram.")

dp = Dispatcher()


async def save_group(m: Message, bot: Bot, fac, g):
    prev = get_user(m.chat.id)
    keep_sg = prev and prev["grp"] == g
    u = upsert(m.chat.id, fac=fac, grp=g, name=m.from_user.first_name if m.from_user else "", active=1,
               sg=prev["sg"] if keep_sg else 0)
    await set_menu(bot, m.chat.id, g)
    text = (f"✅ Группа <b>{escape(g)}</b> сохранена.\nБуду присылать пары {off_text(u['off'])} "
            f"и сообщать об изменениях в расписании. Поменять время — /time")
    if SCHED.has_subgroups(fac, g) and not keep_sg:
        text += "\n\nВ расписании группы есть занятия по подгруппам. Выберите свою, чтобы не видеть чужие пары:"
        return await m.answer(text, reply_markup=sg_kb(0))
    await m.answer(text, reply_markup=open_kb(g))


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot):
    await SCHED.refresh()
    name = m.from_user.first_name if m.from_user else ""
    u = upsert(m.chat.id, name=name, active=1)
    if command.args:
        found = SCHED.find_group(command.args)
        if found:
            return await save_group(m, bot, *found)
    if u["grp"]:
        await set_menu(bot, m.chat.id, u["grp"])
        return await m.answer(f"👋 Привет, {escape(name)}! Ваша группа — <b>{escape(u['grp'])}</b> ({sg_text(u['sg'])}).\n"
                              f"Буду присылать пары {off_text(u['off'])}.\n\n{HELP}", reply_markup=open_kb(u["grp"]))
    await m.answer(f"👋 Привет, {escape(name)}! Я бот с расписанием <b>Казанского ГМУ</b>.\n\n"
                   "Пришлите номер своей группы, например <b>1201</b>, или найдите её в расписании по кнопке ниже.",
                   reply_markup=open_kb(None, "🎓 Найти группу"))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.chat.id}</code>")


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    upsert(m.chat.id, active=0)
    await m.answer("🔕 Напоминания и уведомления выключены. Включить снова — /start")


@dp.message(Command("time"))
async def cmd_time(m: Message):
    u = get_user(m.chat.id)
    await m.answer("⏰ Когда присылать расписание?", reply_markup=time_kb(u["off"] if u else DEFAULT_OFF))


@dp.message(Command("subgroup"))
async def cmd_subgroup(m: Message):
    u = get_user(m.chat.id)
    if not u or not u["grp"]:
        return await m.answer("Сначала выберите группу: пришлите её номер, например <b>1201</b>.")
    await SCHED.refresh()
    note = "" if SCHED.has_subgroups(u["fac"], u["grp"]) else "\n\nСейчас в расписании вашей группы нет деления на подгруппы."
    await m.answer(f"👥 Ваша подгруппа в группе <b>{escape(u['grp'])}</b>? Пары другой подгруппы я буду скрывать.{note}",
                   reply_markup=sg_kb(u["sg"] or 0))


@dp.message(Command("changes"))
async def cmd_changes(m: Message):
    u = upsert(m.chat.id)
    on = 0 if u["changes"] else 1
    upsert(m.chat.id, changes=on)
    await m.answer("🔔 Буду сообщать об изменениях в расписании на ближайшую неделю." if on
                   else "🔕 Уведомления об изменениях выключены. Включить — /changes")


@dp.message(Command("group"))
async def cmd_group(m: Message):
    u = get_user(m.chat.id)
    g = u["grp"] if u else None
    await m.answer(f"Сейчас: <b>{escape(g or 'не выбрана')}</b>.\nПришлите номер новой группы, например <b>1201</b>.",
                   reply_markup=open_kb(g, "🎓 Выбрать в расписании"))


async def reply_day(m: Message, delta: int, title: str):
    u = get_user(m.chat.id)
    if not u or not u["grp"]:
        return await m.answer("Сначала выберите группу: пришлите её номер, например <b>1201</b>.")
    await SCHED.refresh()
    d = now_kzn().date() + dt.timedelta(days=delta)
    evs = SCHED.day(u["fac"], u["grp"], d, u["sg"] or 0)
    holiday = d.isoformat() in SCHED.index.get("holidays", [])
    body = format_day(evs) if evs else ("🎉 Праздничный день, занятий нет." if holiday else "😌 Занятий нет.")
    sgp = f", {u['sg']} подгр." if u["sg"] else ""
    await m.answer(f"{title}, {human_date(d)} · группа <b>{escape(u['grp'])}</b>{sgp}\n\n{body}", reply_markup=open_kb(u["grp"]))


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await reply_day(m, 0, "Сегодня")


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    await reply_day(m, 1, "Завтра")


# ---------- для администратора ----------
@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.chat.id not in ADMIN_IDS:
        return await m.answer("Эта команда только для администратора бота.")
    await SCHED.refresh()
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    total = q("SELECT COUNT(*) FROM users")
    active = q("SELECT COUNT(*) FROM users WHERE active=1 AND grp IS NOT NULL")
    nogroup = q("SELECT COUNT(*) FROM users WHERE grp IS NULL")
    stopped = q("SELECT COUNT(*) FROM users WHERE active=0")
    week_ago = (now_kzn() - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    new_week = q("SELECT COUNT(*) FROM users WHERE created >= ?", week_ago)
    today = now_kzn().date().isoformat()
    st = {r["kind"]: r["n"] for r in db.execute("SELECT kind, n FROM stats WHERE date=?", (today,))}
    st7 = {r["kind"]: r["n"] for r in db.execute(
        "SELECT kind, SUM(n) n FROM stats WHERE date >= ? GROUP BY kind", ((now_kzn().date() - dt.timedelta(days=6)).isoformat(),))}
    by_fac = db.execute("SELECT fac, COUNT(*) n FROM users WHERE active=1 AND grp IS NOT NULL GROUP BY fac ORDER BY n DESC").fetchall()
    by_course = {}
    for r in db.execute("SELECT fac, grp FROM users WHERE active=1 AND grp IS NOT NULL"):
        c = SCHED.course_of(r["fac"], r["grp"])
        by_course[c] = by_course.get(c, 0) + 1
    top = db.execute("SELECT grp, COUNT(*) n FROM users WHERE active=1 AND grp IS NOT NULL GROUP BY grp ORDER BY n DESC LIMIT 5").fetchall()
    evening = q("SELECT COUNT(*) FROM users WHERE active=1 AND off=?", EVENING)
    status = SCHED.status or {}
    lines = [
        "📊 <b>Статистика бота</b>",
        f"Всего пользователей: <b>{total}</b> (за неделю +{new_week})",
        f"Получают напоминания: <b>{active}</b> · без группы: {nogroup} · выключили: {stopped}",
        f"Режим «вечером накануне»: {evening}",
        "",
        f"Сегодня отправлено: напоминаний {st.get('reminder', 0)}, об изменениях {st.get('change', 0)}",
        f"За 7 дней: напоминаний {st7.get('reminder', 0)}, об изменениях {st7.get('change', 0)}, "
        f"команд /today и /tomorrow {st7.get('cmd_day', 0)}",
        "",
        "<b>По направлениям:</b>",
        *[f"• {escape(SCHED.fac_name(r['fac']))}: {r['n']}" for r in by_fac],
        "",
        "<b>По курсам:</b> " + ", ".join(f"{c} к. — {n}" for c, n in sorted(by_course.items())),
        "<b>Топ групп:</b> " + ", ".join(f"{escape(r['grp'])} ({r['n']})" for r in top),
        "",
        f"Данные расписания: {escape(str(SCHED.index.get('updated', '?'))[:16].replace('T', ' '))} UTC · "
        + ("✅ парсер в порядке" if status.get("ok", True) else "⚠️ " + escape(status.get("message", ""))),
    ]
    await m.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("off:"))
async def cb_off(q: CallbackQuery):
    off = int(q.data.split(":")[1])
    upsert(q.message.chat.id, off=off, active=1)
    try:
        await q.message.edit_reply_markup(reply_markup=time_kb(off))
    except Exception:  # noqa: BLE001
        pass
    await q.answer("Буду присылать " + off_text(off))


@dp.callback_query(F.data.startswith("sg:"))
async def cb_sg(q: CallbackQuery):
    sg = int(q.data.split(":")[1])
    u = upsert(q.message.chat.id, sg=sg)
    try:
        await q.message.edit_reply_markup(reply_markup=sg_kb(sg))
    except Exception:  # noqa: BLE001
        pass
    await q.answer("Показываю: " + sg_text(sg))
    # снимок изменений для новой подгруппы — без уведомления
    snapshot_group(u["fac"], u["grp"], sg, notify=False)


@dp.message(F.text)
async def any_text(m: Message, bot: Bot):
    await SCHED.refresh()
    found = SCHED.find_group(m.text)
    if found:
        return await save_group(m, bot, *found)
    await m.answer(f"Не нашёл группу «{escape(m.text[:20])}». Пришлите номер как в расписании, например <b>1201</b> или <b>1110и</b>.",
                   reply_markup=open_kb(None, "🎓 Найти группу"))


# ---------- рассылка напоминаний ----------
def due_message(u, now: dt.datetime):
    """Возвращает текст напоминания, если его пора отправить, иначе None ("" — пометить без отправки)."""
    today, cur = now.date(), now.hour * 60 + now.minute
    if u["sent"] == today.isoformat():
        return None
    name = f", {escape(u['name'])}" if u["name"] else ""
    sg = u["sg"] or 0
    if u["off"] == EVENING:
        if cur < EVENING_AT:
            return None
        tmr = today + dt.timedelta(days=1)
        evs = SCHED.day(u["fac"], u["grp"], tmr, sg)
        if not evs:
            return ""
        return (f"🌙 <b>Добрый вечер{name}!</b>\nЗавтра, {human_date(tmr)}, первая пара в <b>{evs[0]['s']}</b>.\n\n"
                f"{format_day(evs)}")
    evs = SCHED.day(u["fac"], u["grp"], today, sg)
    if not evs:
        return None
    first = mins(evs[0]["s"])
    if not (first - (u["off"] or DEFAULT_OFF) <= cur < first):
        return None
    left = first - cur
    left_s = (f"{left // 60} ч " if left >= 60 else "") + (f"{left % 60} мин" if left % 60 else "")
    greet = "☀️ <b>Доброе утро" if cur < 12 * 60 else "👋 <b>Добрый день"
    return (f"{greet}{name}!</b>\nСегодня первая пара в <b>{evs[0]['s']}</b> — через {left_s.strip()}.\n\n"
            f"{format_day(evs)}")


async def safe_send(bot: Bot, chat_id, text, kb=None) -> bool:
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
            await asyncio.sleep(0.05)  # ≤ 20 сообщений/сек — в пределах лимитов Telegram
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:  # пользователь заблокировал бота
            upsert(chat_id, active=0)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("send %s: %s", chat_id, e)
            return False
    return False


async def send_reminders(bot: Bot):
    now = now_kzn()
    for u in db.execute("SELECT * FROM users WHERE active=1 AND grp IS NOT NULL").fetchall():
        text = due_message(u, now)
        if text is None:
            continue
        if text and await safe_send(bot, u["chat_id"], text, open_kb(u["grp"])):
            stat_inc("reminder")
        upsert(u["chat_id"], sent=now.date().isoformat())


# ---------- изменения в расписании ----------
def fingerprint(evs) -> str:
    return json.dumps([[e["s"], e["e"], e["t"], e.get("l", ""), e.get("p", ""), e.get("k", "")] for e in evs], ensure_ascii=False)


def diff_text(old_fp: str, new_evs) -> str:
    old = json.loads(old_fp)
    new = json.loads(fingerprint(new_evs))
    removed = [x for x in old if x not in new]
    added = [x for x in new if x not in old]
    line = lambda x: f"{x[0]}–{x[1]} {escape(x[2])}" + (f" ({escape(x[3])})" if x[3] else "")  # noqa: E731
    out = [f"❌ <s>{line(x)}</s>" for x in removed] + [f"✅ {line(x)}" for x in added]
    if not new:
        out.append("😌 Теперь занятий нет")
    return "\n".join(out)


def snapshot_group(fac, grp, sg, notify=True):
    """Сравнивает ближайшие дни со снимком. Возвращает [(date, diff)] изменений."""
    today = now_kzn().date()
    changes = []
    for i in range(CHANGE_DAYS):
        d = today + dt.timedelta(days=i)
        evs = SCHED.day(fac, grp, d, sg)
        fp = fingerprint(evs)
        row = db.execute("SELECT fp FROM snap WHERE grp=? AND sg=? AND date=?", (grp, sg, d.isoformat())).fetchone()
        if row and row["fp"] != fp and notify:
            changes.append((d, diff_text(row["fp"], evs)))
        if not row or row["fp"] != fp:
            db.execute("INSERT OR REPLACE INTO snap(grp, sg, date, fp) VALUES(?, ?, ?, ?)", (grp, sg, d.isoformat(), fp))
    db.execute("DELETE FROM snap WHERE date < ?", (today.isoformat(),))
    db.commit()
    return changes


async def check_changes(bot: Bot):
    targets = db.execute("SELECT DISTINCT fac, grp, COALESCE(sg, 0) sg FROM users WHERE active=1 AND grp IS NOT NULL").fetchall()
    for t in targets:
        changes = snapshot_group(t["fac"], t["grp"], t["sg"])
        if not changes:
            continue
        sgp = f", {t['sg']} подгр." if t["sg"] else ""
        text = f"⚠️ <b>Изменилось расписание группы {escape(t['grp'])}{sgp}</b>\n"
        for d, diff in changes[:5]:
            text += f"\n<b>{DOW_SHORT[d.weekday()].capitalize()}, {d.day} {MON[d.month - 1]}</b>\n{diff}\n"
        if len(changes) > 5:
            text += f"\n…и ещё {len(changes) - 5} дн."
        users = db.execute("SELECT chat_id FROM users WHERE active=1 AND changes=1 AND grp=? AND COALESCE(sg, 0)=?",
                           (t["grp"], t["sg"])).fetchall()
        for u in users:
            if await safe_send(bot, u["chat_id"], text, open_kb(t["grp"])):
                stat_inc("change")


# ---------- тревога для администратора ----------
async def check_status(bot: Bot):
    st = SCHED.status
    if not st or not ADMIN_IDS:
        return
    key = f"{st.get('ok')}|{st.get('message')}"
    if key == kv_get("last_status"):
        return
    was_bad = (kv_get("last_status") or "True").startswith("False")
    kv_set("last_status", key)
    if not st.get("ok"):
        probs = "\n".join("• " + escape(p) for p in st.get("problems", [])[:10])
        text = (f"🚨 <b>Проблема с обновлением расписания</b>\n{escape(st.get('message', ''))}\n\n{probs}\n\n"
                f"Сайт и бот продолжают показывать прошлые данные. Проверьте вкладку Actions в репозитории.")
    elif was_bad:
        text = f"✅ Обновление расписания снова работает: {escape(st.get('message', ''))}"
    else:
        return
    for a in ADMIN_IDS:
        await safe_send(bot, a, text)


async def main_loop(bot: Bot):
    seen_version, seen_date = -1, None
    while True:
        try:
            await SCHED.refresh()
            today = now_kzn().date()
            if SCHED.version != seen_version or today != seen_date:
                first = seen_version == -1
                if first:  # после перезапуска только обновляем снимки
                    for t in db.execute("SELECT DISTINCT fac, grp, COALESCE(sg, 0) sg FROM users WHERE grp IS NOT NULL"):
                        snapshot_group(t["fac"], t["grp"], t["sg"], notify=False)
                else:
                    await check_changes(bot)
                seen_version, seen_date = SCHED.version, today
            await check_status(bot)
            await send_reminders(bot)
        except Exception as e:  # noqa: BLE001
            log.exception("main loop: %s", e)
        await asyncio.sleep(60 - dt.datetime.now().second)  # раз в минуту, в начале минуты


@dp.message.outer_middleware()
async def count_cmds(handler, event: Message, data):
    if event.text in ("/today", "/tomorrow"):
        stat_inc("cmd_day")
    return await handler(event, data)


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await SCHED.refresh(force=True)
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands([
        BotCommand(command="today", description="Пары на сегодня"),
        BotCommand(command="tomorrow", description="Пары на завтра"),
        BotCommand(command="time", description="Когда присылать напоминание"),
        BotCommand(command="subgroup", description="Моя подгруппа"),
        BotCommand(command="group", description="Сменить группу"),
        BotCommand(command="changes", description="Уведомления об изменениях"),
        BotCommand(command="stop", description="Выключить напоминания"),
        BotCommand(command="help", description="Помощь"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_url())))
    await bot.set_my_description("Расписание Казанского ГМУ: напоминание о парах по утрам, уведомления об изменениях и расписание всех групп прямо в Telegram.")
    log.info("Бот @%s запущен. Администраторы: %s", BOT_USERNAME, ", ".join(map(str, ADMIN_IDS)) or "не заданы (ADMIN_IDS)")
    asyncio.create_task(main_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
