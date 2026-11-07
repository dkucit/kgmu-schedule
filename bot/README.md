# Пары КГМУ — Telegram-бот

* Каждое утро присылает пары за 2 часа до первой (меняется командой /time: 1–3 ч или вечером накануне в 20:00).
* /today, /tomorrow, /group, /stop; группу можно просто прислать номером.
* Кнопка «Расписание» открывает сайт как мини-приложение сразу на вашей группе.
  Если в мини-приложении выбрать другую группу — появится кнопка «Присылать для …», которая переключит бота.
* Сообщает об изменениях в расписании на ближайшие 7 дней (/changes — выключить).
* Подгруппа: /subgroup — пары другой подгруппы скрываются.
* Для администратора (ADMIN_IDS в .env): /stats — статистика; тревога в личку, если обновление расписания сломалось.
* Работает через long polling: не нужны домен, HTTPS и открытые порты. Подписчики — в SQLite.
* Расписание берёт с сайта (SITE_URL) и перечитывает раз в 30 минут. Сайт обновляется GitHub Actions 3 раза в день.

## Запуск через Docker (проще всего)
```bash
git clone https://github.com/dkucit/kgmu-schedule.git && cd kgmu-schedule/bot
cp .env.example .env      # впишите BOT_TOKEN и ADMIN_IDS
docker compose up -d --build
docker compose logs -f    # должно быть «Бот @... запущен»
```
Обновить бота: `git pull && docker compose up -d --build`.
Свой ID узнаёте командой /id у бота → вписываете в ADMIN_IDS → `docker compose up -d`.

## Запуск без Docker (Ubuntu/Debian)
```bash
sudo useradd -r -m kgmu
sudo mkdir -p /opt/kgmu-bot && sudo cp bot.py requirements.txt kgmu-bot.service .env.example /opt/kgmu-bot/
cd /opt/kgmu-bot && sudo cp .env.example .env && sudo nano .env   # BOT_TOKEN, DB_PATH=/opt/kgmu-bot/kgmu_bot.sqlite3
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo chown -R kgmu:kgmu /opt/kgmu-bot
sudo cp kgmu-bot.service /etc/systemd/system/ && sudo systemctl enable --now kgmu-bot
journalctl -u kgmu-bot -f
```

Нужен Python 3.10+ и доступ сервера к api.telegram.org и dkucit.github.io.
Резервная копия подписчиков — файл базы (data/kgmu_bot.sqlite3).
