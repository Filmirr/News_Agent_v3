#!/usr/bin/env python3
"""Собирает свежие новости из RSS-лент по темам и шлёт дайджест в Telegram.

Запускается раз в сутки (например, в 10:00). Берёт новости за последние
`lookback_hours` часов, группирует по темам из config.yaml и отправляет
одним (или несколькими, если длинно) сообщением в Telegram.

Никаких платных API: только RSS + бесплатный Telegram Bot API.
"""

import calendar
import html
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

# Чтобы эмодзи в логах не роняли скрипт в консоли Windows (cp1251).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import feedparser
import requests
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4000  # лимит Telegram 4096, оставляем запас

# Язык/регион выдачи Google News. Меняется в config.yaml (необязательно).
GNEWS_DEFAULTS = {"hl": "ru", "gl": "RU", "ceid": "RU:ru"}


def google_news_url(query: str, locale: dict) -> str:
    """Строит RSS-ссылку Google News из обычного текстового запроса."""
    params = f"hl={locale['hl']}&gl={locale['gl']}&ceid={locale['ceid']}"
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&{params}"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def entry_datetime(entry) -> datetime | None:
    """Дата публикации записи в UTC, либо None если её нет."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def matches_keywords(entry, keywords: list[str]) -> bool:
    """True если ключевых слов нет, либо хотя бы одно встречается в тексте."""
    if not keywords:
        return True
    haystack = (
        getattr(entry, "title", "") + " " + getattr(entry, "summary", "")
    ).lower()
    return any(kw.lower() in haystack for kw in keywords)


def topic_sources(topic: dict, locale: dict) -> list[dict]:
    """Список источников темы: [{url, apply_keywords}, ...].

    - google_news: запрос уже сам фильтрует → keywords НЕ применяем.
    - feeds: обычные (часто общие) ленты → keywords применяем, если заданы.
    """
    sources: list[dict] = []
    query = topic.get("google_news")
    if query:
        sources.append({"url": google_news_url(query, locale), "apply_keywords": False})
    for feed_url in topic.get("feeds") or []:
        sources.append({"url": feed_url, "apply_keywords": True})
    return sources


def collect_topic(topic: dict, cutoff: datetime, max_items: int, locale: dict) -> list[dict]:
    """Возвращает список свежих новостей по теме: [{title, link, dt}, ...]."""
    keywords = topic.get("keywords") or []
    seen_links: set[str] = set()
    items: list[dict] = []

    for source in topic_sources(topic, locale):
        parsed = feedparser.parse(source["url"])
        if parsed.bozo and not parsed.entries:
            print(f"  ⚠️  не удалось прочитать ленту: {source['url']} ({parsed.bozo_exception})")
            continue

        for entry in parsed.entries:
            dt = entry_datetime(entry)
            if dt is not None and dt < cutoff:
                continue  # новость старше окна
            # Ключевые слова — только для обычных лент; запрос Google News
            # уже отфильтровал результаты сам.
            if source["apply_keywords"] and not matches_keywords(entry, keywords):
                continue
            link = getattr(entry, "link", "")
            title = getattr(entry, "title", "").strip()
            if not link or not title or link in seen_links:
                continue
            seen_links.add(link)
            items.append({"title": title, "link": link, "dt": dt})

    # свежие сверху; новости без даты — в конец
    items.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[:max_items]


def build_messages(config: dict) -> list[str]:
    lookback = int(config.get("lookback_hours", 24))
    max_items = int(config.get("max_items_per_topic", 15))
    locale = {**GNEWS_DEFAULTS, **(config.get("google_news_locale") or {})}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)

    header = (
        f"<b>📰 Сводка новостей</b>\n"
        f"<i>за последние {lookback} ч.</i>"
    )

    # Собираем единый список строк: заголовок темы, затем её новости.
    lines: list[str] = [header]
    total = 0
    for topic in config.get("topics", []):
        print(f"Тема: {topic['name']}")
        items = collect_topic(topic, cutoff, max_items, locale)
        print(f"  найдено: {len(items)}")
        if not items:
            continue
        total += len(items)
        lines.append(f"\n<b>{html.escape(topic['name'])}</b>")
        for it in items:
            lines.append(f'• <a href="{html.escape(it["link"], quote=True)}">' f'{html.escape(it["title"])}</a>' )
            lines.append("")

    if total == 0:
        return [header + "\n\nСвежих новостей по вашим темам не найдено 🤷"]

    # Упаковываем строки в сообщения, не превышая лимит длины Telegram.
    messages: list[str] = []
    current = ""
    for line in lines:
        addition = ("\n" if current else "") + line
        if current and len(current) + len(addition) > MAX_MESSAGE_LEN:
            messages.append(current)
            current = ""
            addition = line
        current += addition
    if current.strip():
        messages.append(current)
    return messages


def send_message(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"❌ Telegram вернул {resp.status_code}: {resp.text}")
        resp.raise_for_status()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ Заданы не все переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
        return 1

    config = load_config()
    messages = build_messages(config)
    for msg in messages:
        send_message(token, chat_id, msg)
        time.sleep(1)  # не спамим API слишком быстро
    print(f"✅ Отправлено сообщений: {len(messages)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
