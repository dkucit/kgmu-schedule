"""Пары КГМУ — Telegram-бот с напоминаниями о парах.

Запуск:  BOT_TOKEN=123:ABC python bot.py
Работает через long polling: не нужны домен, HTTPS и открытые порты.
Данные расписания берёт с сайта (GitHub Pages), подписчиков хранит в SQLite.
"""
import asyncio
import datetime as dt
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
TZ = dt.timezone(dt.timedelta(hours=3))  # Казань
DEFAULT_OFF = 120  # минут до первой пары
EVENING = -1       # режим «вечером накануне»
EVENING_AT = 20 * 60
DATA_TTL = 30 * 60

log = logging.getLogger("kgmu")
DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
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


# ---------- данные расписания ----------
class Schedule:
    def __init__(self):
        self.index = None
        self.facs: dict = {}
        self.loaded_at = 0.0
        self.lock = asyncio.Lock()

    async def refresh(self, force=False):
        async with self.lock:
            if not force and self.index and time.time() - self.loaded_at < DATA_TTL:
                return
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(SITE + "data/index.json") as r:
                    r.raise_for_status()
                    index = await r.json(content_type=None)
                facs = {}
                for f in index["faculties"]:
                    if not f["courses"]:
                        continue
                    old = (self.index or {}).get("_hash", {}).get(f["id"])
                    if old == f.get("hash") and f["id"] in self.facs:
                        facs[f["id"]] = self.facs[f["id"]]
                        continue
                    async with s.get(f"{SITE}data/{f['id']}.json", params={"v": f.get("hash", "")}) as r:
                        r.raise_for_status()
                        facs[f["id"]] = (await r.json(content_type=None))["groups"]
            index["_hash"] = {f["id"]: f.get("hash") for f in index["faculties"]}
            self.index, self.facs, self.loaded_at = index, facs, time.time()
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

    def week_no(self, d: dt.date) -> int:
        start = dt.date.fromisoformat(self.index["weekStart"])
        monday = d - dt.timedelta(days=d.weekday())
        return (monday - start).days // 7 + 1

    def day(self, fac: str, g: str, d: dt.date):
        if not self.index or d.isoformat() in self.index.get("holidays", []):
            return []
        grp = self.facs.get(fac, {}).get(g)
        if not grp:
            return []
        iso, wd, w = d.isoformat(), d.weekday(), self.week_no(d)
        evs = [e for e in grp["ev"]
               if (e["date"] == iso if "date" in e else e.get("d") == wd and (not e.get("w") or w in e["w"]))]
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


# ---------- база подписчиков ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS users(
  chat_id INTEGER PRIMARY KEY, name TEXT, fac TEXT, grp TEXT,
  off INTEGER DEFAULT 120, active INTEGER DEFAULT 1, sent TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
db.commit()


def get_user(chat_id):
    return db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def upsert(chat_id, **kw):
    db.execute("INSERT OR IGNORE INTO users(chat_id, off) VALUES(?, ?)", (chat_id, DEFAULT_OFF))
    if kw:
        db.execute(f"UPDATE users SET {', '.join(k + '=?' for k in kw)} WHERE chat_id=?", (*kw.values(), chat_id))
    db.commit()
    return get_user(chat_id)


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


async def set_menu(bot: Bot, chat_id, g):
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_url(g))))
    except Exception as e:  # noqa: BLE001
        log.warning("menu button: %s", e)


HELP = ("Я присылаю расписание <b>Казанского ГМУ</b>.\n\n"
        "• Пришлите номер группы (например <b>1201</b>), чтобы выбрать или сменить её\n"
        "/today — пары на сегодня\n/tomorrow — пары на завтра\n"
        "/time — когда присылать напоминание\n/stop — выключить напоминания\n/start — включить снова\n\n"
        "Кнопка «Расписание» внизу открывает расписание прямо в Telegram.")

dp = Dispatcher()


async def save_group(m: Message, bot: Bot, fac, g):
    u = upsert(m.chat.id, fac=fac, grp=g, name=m.from_user.first_name if m.from_user else "", active=1)
    await set_menu(bot, m.chat.id, g)
    await m.answer(f"✅ Группа <b>{escape(g)}</b> сохранена.\nБуду присылать пары {off_text(u['off'])}. Поменять время — /time",
                   reply_markup=open_kb(g))


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
        return await m.answer(f"👋 Привет, {escape(name)}! Ваша группа — <b>{escape(u['grp'])}</b>.\n"
                              f"Буду присылать пары {off_text(u['off'])}.\n\n{HELP}", reply_markup=open_kb(u["grp"]))
    await m.answer(f"👋 Привет, {escape(name)}! Я бот с расписанием <b>Казанского ГМУ</b>.\n\n"
                   "Пришлите номер своей группы, например <b>1201</b>, или найдите её в расписании по кнопке ниже.",
                   reply_markup=open_kb(None, "🎓 Найти группу"))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    upsert(m.chat.id, active=0)
    await m.answer("🔕 Напоминания выключены. Включить снова — /start")


@dp.message(Command("time"))
async def cmd_time(m: Message):
    u = get_user(m.chat.id)
    await m.answer("⏰ Когда присылать расписание?", reply_markup=time_kb(u["off"] if u else DEFAULT_OFF))


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
    evs = SCHED.day(u["fac"], u["grp"], d)
    holiday = d.isoformat() in SCHED.index.get("holidays", [])
    body = format_day(evs) if evs else ("🎉 Праздничный день, занятий нет." if holiday else "😌 Занятий нет.")
    await m.answer(f"{title}, {human_date(d)} · группа <b>{escape(u['grp'])}</b>\n\n{body}", reply_markup=open_kb(u["grp"]))


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await reply_day(m, 0, "Сегодня")


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    await reply_day(m, 1, "Завтра")


@dp.callback_query(F.data.startswith("off:"))
async def cb_off(q: CallbackQuery):
    off = int(q.data.split(":")[1])
    upsert(q.message.chat.id, off=off, active=1)
    try:
        await q.message.edit_reply_markup(reply_markup=time_kb(off))
    except Exception:  # noqa: BLE001
        pass
    await q.answer("Буду присылать " + off_text(off))


@dp.message(F.text)
async def any_text(m: Message, bot: Bot):
    await SCHED.refresh()
    found = SCHED.find_group(m.text)
    if found:
        return await save_group(m, bot, *found)
    await m.answer(f"Не нашёл группу «{escape(m.text[:20])}». Пришлите номер как в расписании, например <b>1201</b> или <b>1110и</b>.",
                   reply_markup=open_kb(None, "🎓 Найти группу"))


# ---------- рассылка ----------
def due_message(u, now: dt.datetime):
    """Возвращает текст напоминания, если его пора отправить, иначе None."""
    today, cur = now.date(), now.hour * 60 + now.minute
    if u["sent"] == today.isoformat():
        return None
    name = f", {escape(u['name'])}" if u["name"] else ""
    if u["off"] == EVENING:
        if cur < EVENING_AT:
            return None
        tmr = today + dt.timedelta(days=1)
        evs = SCHED.day(u["fac"], u["grp"], tmr)
        if not evs:
            return ""  # пометить отправленным, но ничего не слать
        return (f"🌙 <b>Добрый вечер{name}!</b>\nЗавтра, {human_date(tmr)}, первая пара в <b>{evs[0]['s']}</b>.\n\n"
                f"{format_day(evs)}")
    evs = SCHED.day(u["fac"], u["grp"], today)
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


async def reminder_loop(bot: Bot):
    while True:
        try:
            await SCHED.refresh()
            now = now_kzn()
            users = db.execute("SELECT * FROM users WHERE active=1 AND grp IS NOT NULL").fetchall()
            for u in users:
                text = due_message(u, now)
                if text is None:
                    continue
                if text:
                    try:
                        await bot.send_message(u["chat_id"], text, reply_markup=open_kb(u["grp"]))
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1)
                        continue
                    except TelegramForbiddenError:  # пользователь заблокировал бота
                        upsert(u["chat_id"], active=0)
                        continue
                    except Exception as e:  # noqa: BLE001
                        log.warning("send %s: %s", u["chat_id"], e)
                        continue
                    await asyncio.sleep(0.05)  # ≤ 20 сообщений/сек — в пределах лимитов Telegram
                upsert(u["chat_id"], sent=now.date().isoformat())
        except Exception as e:  # noqa: BLE001
            log.exception("reminder loop: %s", e)
        await asyncio.sleep(60 - dt.datetime.now().second)  # раз в минуту, в начале минуты


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
        BotCommand(command="group", description="Сменить группу"),
        BotCommand(command="stop", description="Выключить напоминания"),
        BotCommand(command="help", description="Помощь"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Расписание", web_app=WebAppInfo(url=app_url())))
    await bot.set_my_description("Расписание Казанского ГМУ: напоминание о парах по утрам и расписание всех групп прямо в Telegram.")
    log.info("Бот @%s запущен", BOT_USERNAME)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
