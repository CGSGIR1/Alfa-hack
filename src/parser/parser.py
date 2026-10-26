"""
Парсер деловых новостей для хакатона.

Что делает:
  1. Читает RSS-ленты деловых разделов (экономика, бизнес, финансы).
  2. Пропускает новости, которые уже есть в базе (та же ссылка). Одно событие часто
     пишут несколько изданий — такие новости сохраняются все, а в колонке event_id
     у них одно и то же значение.
  3. Для каждой НОВОЙ новости скачивает страницу и достаёт ПОЛНЫЙ текст статьи.
     Если полный текст не достался или статья закрыта пейволлом, сохраняет то, что есть,
     и честно помечает это в колонке is_full = 0 (1 — полный текст), а причину пишет
     в fetch_error.
  4. Сохраняет всё в базу SQLite (news.db) и готовит данные для разметки: дату в UTC
     (published_utc), издание, рубрику ленты и раздел сайта (outlet, rubric, url_section),
     заголовок без подзаголовка (title и subtitle; как в RSS — title_raw), очищенный текст
     (body_clean), лид (lead), текст для эмбеддинга (embed_text) и его хэш (content_hash).
     Очистка текста — в cleaning.py, поиск событий — в events.py.
  5. Выгружает всю базу в news.csv (все столбцы, полный текст) — можно открыть в Excel.

Запуск:
  python parser.py            # забрать все новые новости из всех лент
  python parser.py --limit 3  # не больше 3 НОВЫХ новостей с каждого источника
  python parser.py --refetch  # ещё раз попробовать скачать полный текст
                              # для новостей, где текст неполный (is_full = 0)
  python parser.py --log      # вывод дописывается в parser.log (так запускает автозапуск)

Базу, собранную до появления новых колонок, переводит на них migrate.py.

Все файлы парсера (news.db, news.csv, parser.log, parser.lock) лежат рядом с parser.py,
откуда бы его ни запустили. Парсер работает на Windows, macOS и Linux. Автозапуск
каждые 10 минут на любой из них: python autostart.py on / off / status (см. README.md).

В LLM отдавайте только новости с is_full = 1, для эмбеддингов берите embed_text.
"""

import argparse
import csv
import hashlib
import html
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    from .retention import prune_parser, take_storage_lock
except ImportError:  # Direct script launch from integration and standalone CLI.
    from retention import prune_parser, take_storage_lock

if os.name == "nt":  # Windows: блокировка файла через msvcrt
    import msvcrt
else:  # macOS и Linux
    import fcntl

import feedparser
import requests
import trafilatura

from cleaning import (
    build_embed_text, clean_article, clean_body, clean_html, content_hash, detect_paywall,
    find_boilerplate, guess_deck, make_lead, pick_lead, split_source, split_title,
    to_utc_iso, url_section_from_url,
)
from events import cluster_events, event_text

# Деловые источники. Адреса RSS иногда меняются — если источник пишет
# «в ленте: 0», откройте ссылку в браузере и проверьте, что там XML со списком новостей.
FEEDS = {
    "kommersant_economics": "https://www.kommersant.ru/RSS/section-economics.xml",
    "kommersant_business": "https://www.kommersant.ru/RSS/section-business.xml",
    "vedomosti_business": "https://www.vedomosti.ru/rss/rubric/business",
    "vedomosti_economics": "https://www.vedomosti.ru/rss/rubric/economics",
    "vedomosti_finance": "https://www.vedomosti.ru/rss/rubric/finance",
}

# Текст короче этого числа символов не считается полной статьёй.
MIN_FULL_LEN = 500
# Фразы, по которым видно, что сайт ещё дописывает новость.
UNFINISHED_MARKERS = ("материал дополняется", "новость дополняется", "информация уточняется")
# Что пишется в fetch_error у статьи, обрезанной пейволлом.
PAYWALL_ERROR = "пейволл: видно {}% материала"

# Все файлы парсера лежат рядом с этим скриптом, откуда бы его ни запустили.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "news.db")
CSV_PATH = os.path.join(BASE_DIR, "news.csv")
LOCK_PATH = os.path.join(BASE_DIR, "parser.lock")  # пока он заблокирован, второй запуск сразу выходит
LOG_PATH = os.path.join(BASE_DIR, "parser.log")  # сюда дописывает вывод флаг --log (автозапуск)
PAUSE_SECONDS = 1.0  # пауза между запросами, чтобы не нагружать сайты

# Заголовки как у обычного браузера: некоторые сайты отдают пустую страницу
# или ошибку 403 запросам, которые не похожи на браузер.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Раздел Коммерсанта есть только на странице статьи — это первая «хлебная крошка»
# над заголовком («Экономика › Макроэкономика» → «Экономика»). Здесь она переводится
# в те же слова, что у рубрик RSS-лент; незнакомая метка сохраняется как есть.
KOMMERSANT_SECTIONS = {
    "экономика": "economics",
    "индустрии": "business",
    "бизнес": "business",
    "финансы": "finance",
    "политика": "politics",
    "мир": "world",
    "общество": "society",
    "потребительский рынок": "consumer",
    "технологии": "technology",
    "hi-tech": "technology",
}
BREADCRUMB_RE = re.compile(r'class="doc-title-block__breadcrumb"(.*?)</div>', re.S)
CRUMB_RE = re.compile(r"<a\b[^>]*>([^<]+)</a>")


# ---------- база данных ----------

def init_db(conn):
    """Создаёт таблицу и добавляет новые колонки в старую базу, если их нет."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id         TEXT PRIMARY KEY,   -- хэш ссылки, защищает от дублей
            source     TEXT,
            url        TEXT,
            title      TEXT,
            published  TEXT,
            text       TEXT,               -- ПОЛНЫЙ текст (или анонс, см. is_full)
            parsed_at  TEXT
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(news)")}
    new_columns = {
        "summary": "TEXT",          # анонс из RSS, очищенный от HTML
        "is_full": "INTEGER",       # 1 — полная статья, 0 — анонс, недописанный/обрывочный текст или пейволл
        "text_len": "INTEGER",      # длина text в символах
        "fetch_error": "TEXT",      # почему не удалось скачать полный текст
        # --- для разметки ---
        "published_utc": "TEXT",    # дата публикации в UTC, ISO 8601: сортируется правильно
        "outlet": "TEXT",           # издание: kommersant, vedomosti
        "rubric": "TEXT",           # рубрика RSS-ленты: economics, business, finance
        "url_section": "TEXT",      # раздел сайта: из адреса (Ведомости) или со страницы (Коммерсант)
        "title_raw": "TEXT",        # заголовок как в RSS (у Коммерсанта «Заголовок // Подзаголовок»)
        "subtitle": "TEXT",         # подзаголовок или врезка
        "is_paywalled": "INTEGER",  # 1 — статья обрезана пейволлом («Вы видите N% этого материала»)
        "body_clean": "TEXT",       # текст без повтора заголовка, пейволла и шаблонных абзацев
        "lead": "TEXT",             # первый содержательный абзац body_clean — вместо summary
        "embed_text": "TEXT",       # текст для эмбеддинга: заголовок, подзаголовок, лид, абзацы
        "content_hash": "TEXT",     # sha256(embed_text): поменялся — пересчитать эмбеддинг
        "event_id": "TEXT",         # одно событие — один event_id (id самой ранней новости)
        "is_relevant": "INTEGER",   # относится ли к крупному бизнесу (NULL — ещё не размечено)
        "relevance_score": "REAL",  # оценка релевантности от разметки
    }
    for name, col_type in new_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE news ADD COLUMN {name} {col_type}")

    # переход со старой версии, где была текстовая метка text_source ('full'/'summary')
    if "text_source" in existing:
        conn.execute(
            "UPDATE news SET is_full = CASE WHEN text_source = 'full' THEN 1 ELSE 0 END "
            "WHERE is_full IS NULL"
        )
        try:
            conn.execute("ALTER TABLE news DROP COLUMN text_source")
        except sqlite3.OperationalError:
            pass  # очень старый SQLite не умеет удалять колонки — не страшно

    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_published_utc ON news(published_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_event_id ON news(event_id)")
    conn.commit()


def make_id(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def already_saved(conn, news_id):
    row = conn.execute("SELECT 1 FROM news WHERE id = ?", (news_id,)).fetchone()
    return row is not None


def fetch_dicts(conn, sql, params=()):
    """Результат запроса списком словарей {колонка: значение}."""
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


# ---------- скачивание полного текста ----------

def download(url):
    """Скачивает страницу. Возвращает (html, причина_ошибки); если всё хорошо, причина = None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return "", f"сеть: {type(e).__name__}"

    if resp.status_code != 200:
        return "", f"HTTP {resp.status_code}"  # 403 — сайт блокирует, 404 — нет страницы

    # requests иногда неверно угадывает кодировку русских сайтов
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text, None


def page_section(page_html):
    """Раздел сайта из «хлебных крошек» статьи Коммерсанта; None, если их на странице нет."""
    block = BREADCRUMB_RE.search(page_html or "")
    crumbs = CRUMB_RE.findall(block.group(1)) if block else []
    if not crumbs:
        return None
    label = html.unescape(crumbs[0]).strip().lower()
    return KOMMERSANT_SECTIONS.get(label, label)


def get_full_text(url):
    """
    Скачивает страницу и достаёт основной текст статьи.
    Возвращает (текст, причина_ошибки, раздел_сайта). Если всё хорошо, причина = None.
    Раздел есть только на страницах Коммерсанта (см. page_section), у остальных — None.
    """
    page, error = download(url)
    if error:
        return "", error, None
    section = page_section(page)

    text = trafilatura.extract(
        page,
        url=url,
        favor_recall=True,        # лучше захватить лишнее, чем потерять абзацы
        include_comments=False,
        include_tables=True,
    )
    if not text:
        # частая причина: текст статьи подгружается JavaScript'ом,
        # а в исходном HTML его нет
        return "", "текст не найден на странице", section
    text = clean_article(text)
    if not text:
        return "", "на странице только служебные строки", section
    return text, None, section


def choose_text(full_text, summary, error):
    """
    Решает, что сохранить и как пометить.
    Возвращает (text, summary, is_full, error).

    is_full = 1, только если статья скачалась, она не закрыта пейволлом,
    не короче MIN_FULL_LEN символов, длиннее анонса и не помечена сайтом как недописанная.
    """
    if not full_text:
        return summary, summary, 0, error

    # анонса в RSS нет (например, у «Прайма») — берём первый абзац статьи
    if not summary:
        summary = make_lead(full_text)

    low = full_text.lower()
    if any(marker in low for marker in UNFINISHED_MARKERS):
        return full_text, summary, 0, "материал ещё дополняется"
    visible = detect_paywall(full_text)
    if visible is not None:
        # «Вы видите 10% этого материала» — остальное доступно только подписчикам
        return full_text, summary, 0, PAYWALL_ERROR.format(visible)
    if len(full_text) < MIN_FULL_LEN:
        return full_text, summary, 0, f"текст короче {MIN_FULL_LEN} символов"
    if len(full_text) < len(summary):
        # извлечённый текст короче анонса — значит, извлеклось что-то не то
        return summary, summary, 0, "извлечённый текст короче анонса"
    return full_text, summary, 1, None


# ---------- обработка лент ----------

def rss_time_utc(entry):
    """Дата публикации в UTC из даты, которую разобрал feedparser, — на случай,
    если строку из RSS не разобрал to_utc_iso."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat(timespec="seconds") if t else None


def parse_source(conn, source, feed_url, limit=None, min_date=None):
    """Обрабатывает один источник. Возвращает число новых новостей."""
    feed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
    print(f"[{source}] в ленте: {len(feed.entries)}")
    outlet, rubric = split_source(source)

    added = 0
    skipped = 0
    for e in feed.entries:
        if limit and added >= limit:
            break  # набрали нужное число НОВЫХ новостей
        url = e.get("link")
        title_raw = clean_html(e.get("title", ""))
        if not url:
            continue
        published_utc=to_utc_iso(e.get('published','')) or rss_time_utc(e)
        if min_date and published_utc and published_utc[:10]<min_date:
            continue  # Expired RSS entries must not be downloaded again after cleanup.
        news_id = make_id(url)
        if already_saved(conn, news_id):
            skipped += 1
            continue  # эта новость уже есть в базе

        rss_summary = clean_html(e.get("summary", ""))  # у части лент анонса нет
        full_text, error, section = get_full_text(url)
        text, summary, is_full, error = choose_text(full_text, rss_summary, error)
        title, subtitle = split_title(title_raw)
        if subtitle is None:
            subtitle = guess_deck(rss_summary, text, title_raw)  # врезка Ведомостей
        published = e.get("published", "")

        conn.execute(
            """
            INSERT INTO news (id, source, url, title, published, text, parsed_at,
                              summary, is_full, text_len, fetch_error,
                              published_utc, outlet, rubric, url_section,
                              title_raw, subtitle, is_paywalled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                news_id, source, url, title, published, text,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                summary, is_full, len(text), error,
                published_utc, outlet, rubric,
                url_section_from_url(url) or section,
                title_raw, subtitle, int(detect_paywall(text) is not None),
            ),
        )
        # Commit a cleaned, consumable article immediately. The integration
        # worker can pick it up while the parser downloads the next source.
        publish_article(conn, news_id)
        conn.commit()
        added += 1
        mark = "+" if is_full else "~"
        note = f"  [{len(text)} симв.]" if is_full else f"  [неполный: {error}]"
        print(f"    {mark} {title_raw[:70]}{note}")
        time.sleep(PAUSE_SECONDS)

    print(f"    новых: {added}, пропущено как уже известные: {skipped}")
    return added


def refetch_summaries(conn):
    """Повторно пытается скачать полный текст там, где сохранился только анонс."""
    rows = conn.execute(
        "SELECT id, url, title, COALESCE(summary, ''), COALESCE(text, '') FROM news "
        "WHERE is_full IS NULL OR is_full = 0"
    ).fetchall()
    print(f"Новостей без полного текста: {len(rows)}")

    fixed = 0
    for news_id, url, title, summary, old_text in rows:
        summary = clean_html(summary)
        full_text, error, section = get_full_text(url)
        text, summary, is_full, error = choose_text(full_text, summary, error)
        if not is_full and len(old_text) > len(text):
            text = old_text  # не затираем то, что уже было скачано раньше
        conn.execute(
            "UPDATE news SET text = ?, summary = ?, is_full = ?, text_len = ?, "
            "fetch_error = ?, url_section = COALESCE(url_section, ?) WHERE id = ?",
            (text, summary, is_full, len(text), error, section, news_id),
        )
        conn.commit()
        if is_full:
            fixed += 1
            print(f"    + {title[:70]}  [{len(text)} симв.]")
        else:
            print(f"    ~ {title[:70]}  [{error}]")
        time.sleep(PAUSE_SECONDS)
    print(f"Удалось докачать: {fixed} из {len(rows)}")


# ---------- данные для разметки ----------

# Колонки, которые зависят от всей базы сразу и пересчитываются после каждого запуска.
DERIVED_COLUMNS = ("body_clean", "lead", "embed_text", "content_hash", "is_paywalled", "event_id")


def event_time(row):
    """Время новости для поиска событий: published_utc, а если его нет — parsed_at."""
    for value in (row["published_utc"], row["parsed_at"]):
        if value:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.astimezone()  # без пояса — местное время
    return datetime.fromtimestamp(0, timezone.utc)


def publish_article(conn, news_id):
    """Make this article ready for preparation before the full RSS sweep ends.

    Final corpus-wide boilerplate/event reconciliation still runs at sweep end.
    The raw text is never overwritten or shortened for an embedding limit.
    """
    rows = fetch_dicts(conn, 'SELECT * FROM news WHERE id = ?', (news_id,))
    if not rows:
        return
    row = rows[0]
    body = clean_body(row['text'], row['title'], row['subtitle'], row['title_raw'], {})
    lead = pick_lead(body)
    embed = build_embed_text(row['title'], row['subtitle'], body)
    # Existing event IDs remain useful across independent processing cycles.
    neighbors = fetch_dicts(conn, '''SELECT id,title,subtitle,lead,event_id,published_utc,parsed_at
        FROM news WHERE id != ? AND body_clean IS NOT NULL
        AND abs(julianday(COALESCE(published_utc,parsed_at))-julianday(?)) <= 2''',
        (news_id, row['published_utc'] or row['parsed_at']))
    groups = cluster_events([(n['id'], event_time(n), event_text(n['title'], n['subtitle'], n['lead'])) for n in neighbors] +
                            [(news_id, event_time(row), event_text(row['title'], row['subtitle'], lead))])
    root = groups[news_id]
    for neighbor in neighbors:
        if groups[neighbor['id']] == root:
            root = neighbor['event_id'] or root
            break
    conn.execute('''UPDATE news SET body_clean=?,lead=?,embed_text=?,content_hash=?,is_paywalled=?,event_id=? WHERE id=?''',
        (body, lead, embed, content_hash(embed), int(detect_paywall(row['text']) is not None), root, news_id))


def rebuild_derived(conn):
    """
    Пересчитывает колонки, которые зависят не от одной новости, а от всей базы:
    body_clean, lead, embed_text, content_hash (шаблонные абзацы ищутся по всем
    новостям издания), is_paywalled и event_id (события ищутся среди соседних
    по времени новостей). Записывает только изменившиеся значения.
    Возвращает (сколько строк изменилось, найденные шаблонные абзацы).
    """
    rows = fetch_dicts(
        conn,
        "SELECT id, outlet, title_raw, title, subtitle, text, published_utc, parsed_at, "
        + ", ".join(DERIVED_COLUMNS) + " FROM news ORDER BY id",
    )
    boilerplate = find_boilerplate(rows)

    new = {}
    for r in rows:
        body = clean_body(r["text"], r["title"], r["subtitle"], r["title_raw"],
                          boilerplate.get(r["outlet"], {}))
        embed = build_embed_text(r["title"], r["subtitle"], body)
        new[r["id"]] = {
            "body_clean": body,
            "lead": pick_lead(body),
            "embed_text": embed,
            "content_hash": content_hash(embed),
            "is_paywalled": int(detect_paywall(r["text"]) is not None),
        }
    events = cluster_events([
        (r["id"], event_time(r), event_text(r["title"], r["subtitle"], new[r["id"]]["lead"]))
        for r in rows
    ])

    changed = 0
    for r in rows:
        values = new[r["id"]]
        values["event_id"] = events[r["id"]]
        if any(r[col] != values[col] for col in DERIVED_COLUMNS):
            conn.execute(
                "UPDATE news SET " + ", ".join(f"{col} = ?" for col in DERIVED_COLUMNS)
                + " WHERE id = ?",
                [values[col] for col in DERIVED_COLUMNS] + [r["id"]],
            )
            changed += 1
    conn.commit()
    return changed, boilerplate


def export_csv(conn):
    """Выгружает всю базу в CSV: все столбцы с теми же названиями, полный текст,
    строки в том же порядке, что в базе. Разделитель «;» и UTF-8 с BOM — чтобы Excel
    сразу открыл кириллицу. Абзацы текста разделены переводами строк: Excel показывает
    их внутри ячейки, а в текстовом редакторе одна новость займёт несколько строк."""
    cur = conn.execute("SELECT * FROM news ORDER BY rowid")
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([d[0] for d in cur.description])
        writer.writerows(cur)


def take_lock():
    """
    Не даёт работать двум запускам одновременно (автозапуск по расписанию и ручной,
    парсер и миграция): блокирует LOCK_PATH до конца процесса. На macOS и Linux это
    flock, на Windows — блокировка первого байта файла через msvcrt; так же файл
    блокирует Frontend/tools/build_data.py. Блокировка снимается сама, даже если процесс
    упал. Возвращает открытый файл или None, если её уже держит другой запуск.
    """
    return take_storage_lock(LOCK_PATH)


def utf8_output():
    """
    Печатать всегда в UTF-8. При выводе в файл (например, в лог автозапуска) Windows
    берёт кодировку системы — cp1251, где нет «₽», «→» и других знаков из заголовков,
    и print падает на такой новости.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="сколько НОВЫХ новостей брать с каждого источника")
    ap.add_argument("--refetch", action="store_true",
                    help="докачать полный текст для новостей, где есть только анонс")
    ap.add_argument("--log", action="store_true",
                    help="дописывать весь вывод в parser.log рядом с парсером (так его запускает autostart.py)")
    ap.add_argument("--data-dir", help="отдельная папка для news.db, CSV, лога и блокировки")
    ap.add_argument('--retention-days',type=int,help='удалять и не скачивать новости старше указанного числа дней')
    args = ap.parse_args()
    if args.retention_days is not None and args.retention_days<=0:
        ap.error('--retention-days must be positive')

    if args.data_dir:
        global DB_PATH, CSV_PATH, LOCK_PATH, LOG_PATH
        directory = os.path.abspath(args.data_dir)
        os.makedirs(directory, exist_ok=True)
        DB_PATH = os.path.join(directory, "news.db")
        CSV_PATH = os.path.join(directory, "news.csv")
        LOCK_PATH = os.path.join(directory, "parser.lock")
        LOG_PATH = os.path.join(directory, "parser.log")

    if args.log:
        # построчная запись: лог можно читать, пока парсер работает; ошибки тоже идут сюда
        sys.stdout = sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    else:
        utf8_output()
    print(f"===== {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} запуск парсера")
    lock = take_lock()  # держим файл открытым до конца запуска
    if lock is None:
        print("Предыдущий запуск ещё не закончился — этот пропускаю.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cutoff=None
    if args.retention_days is not None:
        cutoff=(datetime.now(timezone(timedelta(hours=3))).date()-timedelta(days=args.retention_days)).isoformat()
        cleaned=prune_parser(conn,cutoff)
        print(f"Очистка: удалено {cleaned['deleted']} новостей до {cutoff}; резервная копия: {cleaned['backup']}")

    if args.refetch:
        refetch_summaries(conn)
    else:
        total = 0
        for source, url in FEEDS.items():
            try:
                total += parse_source(conn, source, url, args.limit,min_date=cutoff)
            except Exception as e:
                # один упавший источник не должен ронять весь парсер
                print(f"[{source}] ошибка: {e}")
        print(f"\nНовых за запуск: {total}")

    changed, _ = rebuild_derived(conn)
    print(f"Данные для разметки пересчитаны, изменилось строк: {changed}")
    export_csv(conn)
    count, full = conn.execute(
        "SELECT COUNT(*), SUM(is_full = 1) FROM news"
    ).fetchone()
    print(f"Всего в базе: {count}, из них с полным текстом: {full or 0}. Таблица: {CSV_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
