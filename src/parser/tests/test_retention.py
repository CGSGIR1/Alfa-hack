"""Expired RSS items must not be fetched again after storage cleanup."""
import os
import sys
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parser


class ParserRetentionTests(unittest.TestCase):
    def test_expired_entry_is_skipped_before_network_download(self):
        feed=type('Feed',(),{'entries':[dict(link='https://example.org/old',title='Old news',published='Tue, 14 Oct 2026 12:00:00 GMT')]})()
        with closing(sqlite3.connect(':memory:')) as conn:
            parser.init_db(conn)
            with patch.object(parser.feedparser,'parse',return_value=feed),patch.object(parser,'get_full_text',side_effect=AssertionError('Expired article was fetched')):
                self.assertEqual(parser.parse_source(conn,'kommersant_business','https://example.org/rss',min_date='2026-10-15'),0)
            self.assertEqual(conn.execute('SELECT count(*) FROM news').fetchone()[0],0)


if __name__=='__main__':unittest.main()
