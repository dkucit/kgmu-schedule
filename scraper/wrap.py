"""Собирает site/index.html (полноценный HTML) из site/app.html."""
from pathlib import Path
root = Path(__file__).resolve().parent.parent / "site"
body = (root / "app.html").read_text("utf-8")
head_end = body.index("</style>") + len("</style>")
html = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<meta name="theme-color" content="#0d7a63">\n'
        '<script>if(/tgWebApp/.test(location.hash+location.search)||window.TelegramWebviewProxy)document.write(\'<script src="https://telegram.org/js/telegram-web-app.js"><\\/script>\')</script>\n'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}[hidden]{display:none!important}img{max-width:100%}</style>\n'
        + body[:head_end] + "\n</head>\n<body>\n" + body[head_end:] + "\n</body>\n</html>\n")
(root / "index.html").write_text(html, "utf-8")
print("site/index.html ok")
