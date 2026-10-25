"""
Миграция уже собранной базы news.db на колонки для разметки.

Новые записи парсер (parser.py) сразу пишет правильно, а этот скрипт доводит до того же
вида строки, собранные раньше. Из таблицы ничего не удаляется — только добавляются
колонки и флаги. Повторный запуск ничего не дублирует и не перетирает.

Что делает:
  1. Бэкап: news.db → news_backup_<дата>.db (существующий бэкап не перезаписывается).
  2. Добавляет новые колонки и индексы — те же, что создаёт parser.py.
  3. Заполняет базовые колонки там, где они пустые: title_raw / title / subtitle,
     outlet, rubric, url_section (Ведомости — из адреса), published_utc. Переводит
     parsed_at без часового пояса в UTC. У статей за пейволлом ставит is_full = 0.
  4. Докачивает со страниц раздел для статей Коммерсанта (--offline — пропустить).
  5. Пересчитывает body_clean, lead, embed_text, content_hash, is_paywalled, event_id
     и заново выгружает news.csv.
  6. Печатает отчёт и проверки.

Запуск:
  python migrate.py            # бэкап, миграция, отчёт
  python migrate.py --offline  # то же, но без запросов к сайтам
  python migrate.py --report   # только отчёт, база не меняется
"""

import argparse
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import parser as news_parser
from cleaning import (
    PAYWALL_RE, body_paragraphs, detect_paywall, find_boilerplate, guess_deck,
    is_template_line, norm_compare, split_source, split_title, to_utc_iso, url_section_from_url,
)


def backup(db_path):
    """Копирует базу в <имя>_backup_<дата>.db, а если такой бэкап уже есть — в
    <имя>_backup_<дата>_<время>.db. Существующий бэкап никогда не перезаписывается."""
    now = datetime.now()
    base = f"{os.path.splitext(db_path)[0]}_backup_{now:%Y-%m-%d}"
    path = f"{base}.db"
    if os.path.exists(path):
        path = f"{base}_{now:%H%M%S}.db"
    if os.path.exists(path):
        raise SystemExit(f"Бэкап {path} уже есть — запустите миграцию ещё раз через секунду.")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    return path


def local_to_utc(value):
    """parsed_at без часового пояса («2026-10-20T22:52:19», местное время этого компьютера) →
    то же время в UTC («2026-10-20T19:52:19+00:00»). Для значения с поясом — None."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        return None
    return dt.astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")


def backfill_base(conn):
    """Заполняет базовые колонки у старых строк — только там, где они пустые
    (у новых записей их заполнил парсер). Возвращает {что: сколько строк изменено}."""
    done = Counter()
    for r in news_parser.fetch_dicts(conn, "SELECT * FROM news ORDER BY id"):
        updates = {}
        if r["title_raw"] is None:
            title, subtitle = split_title(r["title"])
            if subtitle is None:
                subtitle = guess_deck(r["summary"], r["text"], r["title"])  # врезка Ведомостей
            updates.update(title_raw=r["title"], title=title, subtitle=subtitle)
            done["title_raw / title / subtitle"] += 1

        outlet, rubric = split_source(r["source"])
        if r["outlet"] is None and outlet:
            updates["outlet"] = outlet
            done["outlet"] += 1
        if r["rubric"] is None and rubric:
            updates["rubric"] = rubric
            done["rubric"] += 1

        section = url_section_from_url(r["url"])
        if r["url_section"] is None and section:
            updates["url_section"] = section
            done["url_section из адреса"] += 1

        published_utc = to_utc_iso(r["published"])
        if r["published_utc"] is None and published_utc:
            updates["published_utc"] = published_utc
            done["published_utc"] += 1

        parsed_at = local_to_utc(r["parsed_at"])
        if parsed_at:
            updates["parsed_at"] = parsed_at
            done["parsed_at → UTC"] += 1

        visible = detect_paywall(r["text"])
        if visible is not None and r["is_full"] != 0:
            updates.update(is_full=0, fetch_error=news_parser.PAYWALL_ERROR.format(visible))
            done["пейволл: is_full = 0"] += 1

        if updates:
            conn.execute(
                "UPDATE news SET " + ", ".join(f"{col} = ?" for col in updates) + " WHERE id = ?",
                [*updates.values(), r["id"]],
            )
    conn.commit()
    return done


def fetch_kommersant_sections(conn):
    """Докачивает раздел сайта для статей Коммерсанта, у которых он пустой: в их адресе
    (/doc/123) раздела нет, он есть только на странице. Возвращает (нашли, всего)."""
    rows = conn.execute(
        "SELECT id, url FROM news WHERE outlet = 'kommersant' AND url_section IS NULL ORDER BY id"
    ).fetchall()
    if rows:
        print(f"Скачиваю страницы Коммерсанта ради раздела сайта: {len(rows)}")
    found = 0
    for news_id, url in rows:
        page, error = news_parser.download(url)
        section = news_parser.page_section(page)
        if section:
            conn.execute("UPDATE news SET url_section = ? WHERE id = ?", (section, news_id))
            conn.commit()
            found += 1
        else:
            print(f"    ~ {url}: {error or 'на странице нет «хлебных крошек»'}")
        time.sleep(news_parser.PAUSE_SECONDS)
    return found, len(rows)


# ---------- отчёт ----------

def print_report(conn):
    """Отчёт: шаблонные абзацы, окна рубрик и проверки из задачи."""
    rows = news_parser.fetch_dicts(conn, "SELECT * FROM news ORDER BY published_utc, id")
    with_sub = [r for r in rows if r["subtitle"]]
    split = sum(1 for r in with_sub if r["title_raw"] != r["title"])
    print(f"\n========== ОТЧЁТ: {len(rows)} новостей ==========")
    print(f"subtitle заполнен у {len(with_sub)}: из « // » — {split}, врезок — {len(with_sub) - split}")

    print("\n--- Шаблонные абзацы: дословно в ≥ 2 новостях одного издания (вырезаны из body_clean)")
    for outlet, paras in sorted(find_boilerplate(rows).items()):
        for p, n in sorted(paras.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  [{outlet}] ×{n}: {p}")

    print("\n--- Служебные строки, снятые по шаблонам (TEMPLATE_LINE_PATTERNS, JUNK_LINE_PATTERNS)")
    removed = Counter()
    for r in rows:
        for p in body_paragraphs(r["text"], r["title"], r["subtitle"], r["title_raw"]):
            if is_template_line(p):
                removed[(r["outlet"], p)] += 1
    for (outlet, p), n in sorted(removed.items()):
        print(f"  [{outlet}] ×{n}: {p}")

    print("\n--- Окна рубрик: что попало в базу из каждой RSS-ленты")
    by_source = defaultdict(list)
    for r in rows:
        if r["published_utc"]:
            by_source[r["source"]].append(r["published_utc"])
    for source, dates in sorted(by_source.items()):
        lo, hi = min(dates), max(dates)
        hours = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).total_seconds() / 3600
        print(f"  {source:21} {len(dates):3} шт.  {lo} … {hi}  (окно {hours:.0f} ч)")

    print("\n========== ПРОВЕРКИ ==========")
    exact = conn.execute(
        "SELECT COUNT(*) FROM news WHERE substr(body_clean, 1, length(title)) = title"
    ).fetchone()[0]
    normalized = sum(1 for r in rows if r["body_clean"]
                     and norm_compare(r["body_clean"]).startswith(norm_compare(r["title"])))
    print(f"1. body_clean начинается с title: {exact} (точно), {normalized} (после нормализации)"
          " — ожидаем 0")

    left = sum(1 for r in rows if PAYWALL_RE.search(r["body_clean"] or ""))
    print(f"2. body_clean содержит «Вы видите … этого материала»: {left} — ожидаем 0")

    paywalled = [r for r in rows if r["is_paywalled"] == 1]
    print(f"3. is_paywalled = 1: {len(paywalled)} — ожидаем 4")
    for r in paywalled:
        print(f"     is_full = {r['is_full']}, {r['fetch_error']} | {r['title']}")

    ordered = conn.execute(
        "SELECT published_utc, published, title FROM news ORDER BY published_utc"
    ).fetchall()
    print("4. ORDER BY published_utc — первые и последние 3:")
    for utc, published, title in ordered[:3] + [("…", "", "")] + ordered[-3:]:
        print(f"     {utc:25} {published:31} {title[:60]}")
    real = [parsedate_to_datetime(published) for _, published, _ in ordered]
    in_order = all(a <= b for a, b in zip(real, real[1:]))
    print(f"   порядок совпадает с настоящим временем публикации: {'да' if in_order else 'НЕТ'}")
    old_min, old_max = conn.execute("SELECT MIN(published), MAX(published) FROM news").fetchone()
    print(f"   для сравнения, по старой колонке published: MIN = {old_min}, MAX = {old_max}")

    mismatch = conn.execute(
        "SELECT source, url_section, title, url FROM news "
        "WHERE url_section IS NOT NULL AND url_section != rubric ORDER BY source, published_utc"
    ).fetchall()
    print(f"5. url_section ≠ rubric: {len(mismatch)}")
    for source, section, title, url in mismatch:
        print(f"     {source:21} → {section:11} {title[:60]}  {url}")
    no_section = conn.execute("SELECT COUNT(*) FROM news WHERE url_section IS NULL").fetchone()[0]
    print(f"   без url_section: {no_section}")

    events = defaultdict(list)
    for r in rows:
        events[r["event_id"]].append(r)
    multi = [members for members in events.values() if len(members) >= 2]
    print(f"6. event_id с ≥ 2 новостями: {len(multi)} (новостей в них: {sum(map(len, multi))})")
    for members in sorted(multi, key=lambda m: m[0]["published_utc"]):
        print(f"   event_id {members[0]['event_id']}")
        for r in members:
            subtitle = f" // {r['subtitle']}" if r["title_raw"] != r["title"] else ""
            print(f"     {r['published_utc'][:16]}  [{r['source']}]  {r['title']}{subtitle}")

    print("7. Три случайных embed_text целиком:")
    for r in random.sample(rows, min(3, len(rows))):
        print(f"\n   --- {r['id']}  [{r['source']}]  {len(r['embed_text'])} симв.")
        print(r["embed_text"])


def main():
    ap = argparse.ArgumentParser(description="Миграция news.db на колонки для разметки")
    ap.add_argument("--offline", action="store_true",
                    help="не скачивать страницы Коммерсанта ради раздела сайта")
    ap.add_argument("--report", action="store_true",
                    help="только отчёт и проверки, база не меняется")
    args = ap.parse_args()

    news_parser.utf8_output()
    db_path = news_parser.DB_PATH
    if not os.path.exists(db_path):
        raise SystemExit(f"Нет базы {db_path}.")

    if args.report:
        # as_uri даёт правильный адрес файла и на Windows (file:///C:/…), и с пробелами в пути
        conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True)
        print_report(conn)
        conn.close()
        return

    lock = news_parser.take_lock()  # держим файл открытым до конца миграции
    if lock is None:
        raise SystemExit("Сейчас работает парсер — запустите миграцию, когда он закончит.")

    print(f"Бэкап: {backup(db_path)}")
    conn = sqlite3.connect(db_path)
    news_parser.init_db(conn)

    done = backfill_base(conn)
    print("Базовые колонки:", ", ".join(f"{k} — {v}" for k, v in done.items())
          or "ничего не изменилось")
    if not args.offline:
        found, total = fetch_kommersant_sections(conn)
        print(f"Раздел Коммерсанта со страниц: найден у {found} из {total}")

    changed, _ = news_parser.rebuild_derived(conn, full=True)
    print(f"body_clean, lead, embed_text, content_hash, is_paywalled, event_id: "
          f"изменилось строк — {changed}")
    news_parser.export_csv(conn)
    print_report(conn)
    conn.close()


if __name__ == "__main__":
    main()
