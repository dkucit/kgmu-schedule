# Пары КГМУ

Расписание Казанского ГМУ для всех факультетов и групп: **https://dkucit.github.io/kgmu-schedule/**

* `site/app.html` — сайт (одна страница). `scraper/wrap.py` собирает из неё `site/index.html`.
* `scraper/build.py` — скачивает Excel-файлы со страницы https://kazangmu.ru/obuchenie/studentam/raspisanie-zanyatij и собирает `site/data/*.json`.
* `.github/workflows/deploy.yml` — GitHub Actions 3 раза в день обновляет данные и публикует сайт на GitHub Pages.
* `bot/` — Telegram-бот: напоминание о парах по утрам и мини-приложение. Запуск — `bot/README.md`.

## Запуск бота на сервере
```bash
git clone https://github.com/dkucit/kgmu-schedule.git
cd kgmu-schedule/bot
cp .env.example .env    # вписать BOT_TOKEN
docker compose up -d --build
```
Обновить бота: `git pull && docker compose up -d --build`.
