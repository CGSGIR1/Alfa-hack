r"""
Тесты пересчёта производных колонок (rebuild_derived): что считается только нужное и что
результат не расходится с полным пересчётом.

Запуск из папки Hackaton:
  .venv/bin/python -m unittest discover -s Parser/tests -v        # macOS, Linux
  .venv\Scripts\python -m unittest discover -s Parser\tests -v    # Windows
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# модули парсера лежат на уровень выше папки tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parser as news_parser
from events import cluster_events

START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# Три темы с непересекающимися словами: внутри темы тексты почти одинаковые, между темами — нет.
TOPICS = {
    "gas": "Газпром сообщил об увеличении поставок трубопроводного газа в Китай по маршруту Сила Сибири",
    "bank": "Центробанк повысил ключевую ставку и объяснил решение ускорением потребительской инфляции",
    "steel": "Металлурги просят правительство снизить экспортные пошлины на прокат и стальные заготовки",
}


def make_db():
    conn = sqlite3.connect(":memory:")
    news_parser.init_db(conn)
    return conn


def add(conn, news_id, topic, hours, outlet="kommersant", extra=""):
    """Новая новость: производные колонки пустые, как после INSERT в parse_source."""
    title = TOPICS[topic]
    body = f"{title}. Подробности: {news_id}. " + "Это содержательный абзац новости. " * 5 + extra
    published = (START + timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO news (id, source, url, title, title_raw, subtitle, published, text, parsed_at, "
        "published_utc, outlet, is_full) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (news_id, outlet + "_economics", "https://x/" + news_id, title, title, None, published,
         title + "\n" + body, published, published, outlet))
    conn.commit()


def events_of(conn):
    return dict(conn.execute("SELECT id, event_id FROM news").fetchall())


class Recorder:
    """Подмена cluster_events, которая запоминает, какие новости ей передали."""

    def __init__(self):
        self.calls = []

    def __call__(self, items, *args, **kwargs):
        self.calls.append([it[0] for it in items])
        return cluster_events(items, *args, **kwargs)


class RebuildTest(unittest.TestCase):
    def db(self):
        conn = make_db()
        self.addCleanup(conn.close)
        return conn

    def test_nothing_new_does_no_work(self):
        conn = self.db()
        add(conn, "a", "gas", 0)
        news_parser.rebuild_derived(conn)
        rec = Recorder()
        with mock.patch.object(news_parser, "cluster_events", rec), \
                mock.patch.object(news_parser, "clean_body", side_effect=AssertionError("не должна вызываться")):
            self.assertEqual(news_parser.rebuild_derived(conn), (0, {}))
        self.assertEqual(rec.calls, [])

    def test_old_news_are_not_reclustered(self):
        conn = self.db()
        for i in range(5):
            add(conn, f"old{i}", "gas", i)            # старые, вне окна новых
        news_parser.rebuild_derived(conn)
        add(conn, "new", "bank", 24 * 10)              # через 10 суток
        rec = Recorder()
        with mock.patch.object(news_parser, "cluster_events", rec):
            news_parser.rebuild_derived(conn)
        self.assertEqual(rec.calls, [["new"]])

    def test_new_news_joins_recent_event(self):
        conn = self.db()
        add(conn, "a", "gas", 0)
        add(conn, "x", "steel", 1)
        news_parser.rebuild_derived(conn)
        add(conn, "b", "gas", 2)                       # то же событие, ещё не склеено
        news_parser.rebuild_derived(conn)
        ev = events_of(conn)
        self.assertEqual(ev["b"], ev["a"])
        self.assertEqual(ev["a"], "a")                 # id события — самая ранняя новость
        self.assertNotEqual(ev["x"], ev["a"])

    def test_event_started_before_window_stays_closed(self):
        conn = self.db()
        add(conn, "a", "gas", 0)
        add(conn, "b", "gas", 40)                      # событие a+b: началось в час 0
        news_parser.rebuild_derived(conn)
        self.assertEqual(events_of(conn)["b"], "a")
        add(conn, "n", "gas", 80)                      # событие a+b началось в час 0: с n оно растянулось бы на 80 ч > 48 ч
        news_parser.rebuild_derived(conn)
        ev = events_of(conn)
        self.assertEqual(ev["a"], "a")
        self.assertEqual(ev["b"], "a")                 # закрытое событие не тронуто
        self.assertEqual(ev["n"], "n")                 # новая — отдельное событие
        full = self.db()
        for news_id, hours in (("a", 0), ("b", 40), ("n", 80)):
            add(full, news_id, "gas", hours)
        news_parser.rebuild_derived(full, full=True)
        self.assertEqual(ev, events_of(full))          # полный пересчёт даёт то же самое

    def test_full_rebuild_reclusters_everything(self):
        conn = self.db()
        for i in range(3):
            add(conn, f"n{i}", "gas", i * 100)
        news_parser.rebuild_derived(conn)
        rec = Recorder()
        with mock.patch.object(news_parser, "cluster_events", rec):
            news_parser.rebuild_derived(conn, full=True)
        self.assertEqual(sorted(rec.calls[0]), ["n0", "n1", "n2"])

    def test_emptied_body_clean_marks_row_for_recalculation(self):
        conn = self.db()
        add(conn, "a", "gas", 0)
        news_parser.rebuild_derived(conn)
        conn.execute("UPDATE news SET body_clean = NULL WHERE id = 'a'")   # так refetch помечает изменившийся текст
        conn.commit()
        changed, _ = news_parser.rebuild_derived(conn)
        self.assertEqual(changed, 1)
        self.assertTrue(conn.execute("SELECT body_clean FROM news WHERE id = 'a'").fetchone()[0])

    def test_old_news_lose_paragraph_that_became_boilerplate(self):
        """Абзац стал шаблонным из-за новой новости — старая новость (не новая!) должна его потерять."""
        conn = self.db()
        footer = "Справка: подробности ежедневно на сайте издания, подписывайтесь на рассылку об экономике."
        add(conn, "a", "gas", 0, outlet="vedomosti", extra="\n" + footer)
        news_parser.rebuild_derived(conn)
        self.assertIn(footer, conn.execute("SELECT body_clean FROM news WHERE id = 'a'").fetchone()[0])
        add(conn, "b", "bank", 1, outlet="vedomosti", extra="\n" + footer)
        news_parser.rebuild_derived(conn)
        for news_id in ("a", "b"):
            self.assertNotIn(footer, conn.execute("SELECT body_clean FROM news WHERE id = ?", (news_id,)).fetchone()[0])

    def test_incremental_matches_full_rebuild(self):
        """Колонки очистки после поштучного добавления совпадают с полным пересчётом."""
        steps = [("a", "gas", 0, "vedomosti"), ("b", "bank", 3, "vedomosti"), ("c", "gas", 5, "kommersant"),
                 ("d", "steel", 30, "vedomosti"), ("e", "bank", 33, "vedomosti"), ("f", "gas", 90, "kommersant")]
        footer = "\nСправка: подробности ежедневно на сайте издания, подписывайтесь на рассылку об экономике."
        inc, full = self.db(), self.db()
        for news_id, topic, hours, outlet in steps:
            extra = footer if news_id in ("b", "d", "e") else ""
            add(inc, news_id, topic, hours, outlet, extra)
            add(full, news_id, topic, hours, outlet, extra)
            news_parser.rebuild_derived(inc)
        news_parser.rebuild_derived(full, full=True)
        columns = ", ".join(c for c in news_parser.DERIVED_COLUMNS if c != "event_id")
        query = f"SELECT id, {columns} FROM news ORDER BY id"
        self.assertEqual(inc.execute(query).fetchall(), full.execute(query).fetchall())
        self.assertEqual(events_of(inc), events_of(full))


if __name__ == "__main__":
    unittest.main()
