import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parser import init_db, publish_article


class LivePublicationTests(unittest.TestCase):
    def test_cleaned_article_is_visible_before_full_corpus_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'news.db')
            with closing(sqlite3.connect(db)) as writer:
                init_db(writer)
                writer.execute('''INSERT INTO news(id,outlet,url,title,title_raw,text,published_utc,parsed_at,is_full)
                    VALUES(?,?,?,?,?,?,?,?,1)''', ('N1','kommersant','https://example.org/N1','Рост затрат',
                    'Рост затрат','Транспортные расходы производителей выросли. Компании пересматривают закупочные планы.',
                    '2026-10-24T10:00:00Z','2026-10-24T10:05:00Z'))
                publish_article(writer,'N1')
                writer.commit()
                # A different process can now prepare this article immediately.
                with closing(sqlite3.connect(db)) as reader:
                    body,lead,fingerprint,event_id = reader.execute(
                        'SELECT body_clean,lead,content_hash,event_id FROM news WHERE id=?',('N1',)).fetchone()
                self.assertIn('Транспортные расходы',body)
                self.assertTrue(lead)
                self.assertEqual(len(fingerprint),64)
                self.assertEqual(event_id,'N1')


if __name__ == '__main__':unittest.main()
