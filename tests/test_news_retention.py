"""Age boundaries, shared vectors, pending outbox safety and import resurrection."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.importing import import_all,input_id
from news_analytics.retention import cleanup_news,news_cutoff
from news_analytics.web import install_uploads
from news_analytics.workflow import batch_report,history
from src.parser.retention import cleanup_parser,take_storage_lock
from src.integration.site import feed,delivery_day


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service

    def tearDown(self):self.fixture.tearDown()

    def add_news(self,nid,day):
        doc=fixture.helpers.news();doc['document_id']=nid;doc['document_date']=day
        doc['source']['url']='https://example.org/'+nid
        path=self.fixture.root/(nid+'.json');write_json(path,doc)
        import_all(self.service.db,{'news':path});self.fixture.fill_vectors(self.service.db)
        return path,doc

    def test_boundary_shared_vectors_and_analytics_are_preserved(self):
        self.add_news('expired','2026-10-14');self.add_news('boundary','2026-10-15')
        with connect(self.service.db) as c:
            with c:
                orphan=input_id(c,'news_context','An obsolete unreferenced news input')
            preserved={r['id']:r['embedding'] for r in c.execute("SELECT id,embedding FROM embedding_inputs WHERE role NOT IN ('news_context','news_mechanism')")}
        result=cleanup_news(self.service.db,10,'2026-10-25')
        self.assertEqual(result['counts']['news'],1);self.assertTrue(Path(result['backup']).is_file())
        with connect(self.service.db,True) as c:
            self.assertEqual({r[0] for r in c.execute('SELECT id FROM news')},{'N1','boundary'})
            self.assertIsNone(c.execute('SELECT 1 FROM embedding_inputs WHERE id=?',(orphan,)).fetchone())
            self.assertEqual(preserved,{r['id']:r['embedding'] for r in c.execute("SELECT id,embedding FROM embedding_inputs WHERE role NOT IN ('news_context','news_mechanism')")})
            self.assertEqual(c.execute('SELECT count(*) FROM companies').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM analytics').fetchone()[0],1)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        self.assertEqual(cleanup_news(self.service.db,10,'2026-10-25')['counts']['news'],0)

    def test_dry_run_does_not_delete_or_create_backups(self):
        self.add_news('expired','2026-10-14')
        result=self.service.cleanup('2026-10-25',dry_run=True)
        self.assertEqual(result['counts']['news'],1);self.assertIsNone(result['backup'])
        self.assertFalse((self.service.db.parent/'news-retention.json').exists())
        with connect(self.service.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM news').fetchone()[0],2)

    def test_completed_report_snapshot_survives_removal_of_source_news(self):
        document=fixture.helpers.news()
        quote='Затраты торговых компаний выросли из-за удорожания перевозок. Возможность перенести дополнительные затраты в цены зависит от устойчивости спроса покупателей.'
        document['source_blocks']=[dict(id='B1',text=quote)]
        document['event']={'id':'parser-original-event'}
        document['fragments'][0].update(source_block_ids=['B1'],source_quotes=[quote])
        target=self.fixture.root/'news-with-original.json';write_json(target,document)
        import_all(self.service.db,{'news':target},update_existing=True)
        self.fixture.fill_vectors(self.service.db)
        class SourceClient(fixture.FakeClient):
            def chat_json(self,model,messages,task,params=None):
                result=super().chat_json(model,messages,task,params)
                if task in ('report_write','report_revise'):
                    result['items'][0]['bank_view']=[dict(text='Рост затрат может приводить к росту цен, если спрос допускает перенос.',
                        type='third_party_view',refs=['A1_F1'],company_basis=[])]
                return result
        done=self.service.generate('C1','2026-10-23',client=SourceClient())
        before=batch_report(self.service.db,done['saved']['run_id'],'C1')
        card_before=feed(self.service.db,'C1',7,'2026-10-23')['news'][0]
        self.assertEqual(card_before['event_key'],'parser:parser-original-event')
        cleaned=cleanup_news(self.service.db,10,'2026-11-02')
        self.assertEqual(cleaned['counts']['news'],1)
        self.assertEqual(batch_report(self.service.db,done['saved']['run_id'],'C1'),before)
        self.assertEqual(history(self.service.db)[0]['status'],'completed')
        card_after=feed(self.service.db,'C1',7,'2027-05-01')['news'][0]
        self.assertEqual(card_after['feed_id'],card_before['feed_id'])
        for field in ('source_blocks','source','fragments','analytical_source_fragments','narrative'):
            self.assertEqual(card_after[field],card_before[field])
        self.assertTrue(card_after['source_blocks'])
        self.assertTrue(card_after['analytical_source_fragments'])
        with connect(self.service.db,True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM news').fetchone()[0],0)

    def test_feed_period_date_uses_moscow_report_delivery(self):
        self.assertEqual(delivery_day('2026-10-23T22:30:00Z','2026-10-22'),'2026-10-24')
        self.assertEqual(delivery_day(None,'2026-10-22'),'2026-10-22')
        self.assertEqual(delivery_day('invalid','2026-10-22'),'2026-10-22')

    def test_expired_member_cancels_pending_packet_and_releases_fresh_member(self):
        self.add_news('expired','2026-10-14')
        with connect(self.service.db) as c:
            with c:c.execute("UPDATE analytics SET document_date='2026-10-01'")
        self.service.config['retrieval']['news_days']=30
        prepared=self.service.compare('C1','2026-10-23',top_news=2)
        self.assertEqual(len(prepared['report']['news']),2)
        result=cleanup_news(self.service.db,10,'2026-10-25')
        self.assertEqual(result['cancelled_pending_batches'],1)
        self.assertEqual(history(self.service.db)[0]['status'],'cancelled')
        with connect(self.service.db,True) as c:
            self.assertIsNone(c.execute("SELECT assigned_run_id FROM company_news_checks WHERE news_id='N1'").fetchone()[0])
        later=self.service.generate('C1','2026-10-25',client=fixture.FakeClient())
        self.assertEqual(later['status'],'completed')
        self.assertEqual([n['news_id'] for n in later['report']['news']],['N1'])

    def test_import_and_upload_cannot_resurrect_old_news(self):
        path,doc=self.add_news('expired','2026-10-14')
        cleanup_news(self.service.db,10,'2026-10-25')
        result=import_all(self.service.db,{'news':path},min_news_date='2026-10-15')
        self.assertEqual(result['kinds']['news']['expired_skipped'],1)
        folders={kind:self.fixture.root/('uploads-'+kind) for kind in ('companies','news','analytics')}
        uploaded=install_uploads(self.service.db,folders,[dict(name='old.json',content=json.dumps(doc))],min_news_date='2026-10-15')
        self.assertEqual(len(uploaded['expired_news']),1)
        self.assertFalse(folders['news'].exists())
        self.assertEqual(self.service.import_inputs({'news':path})['kinds']['news']['expired_skipped'],1)
        with connect(self.service.db,True) as c:self.assertIsNone(c.execute("SELECT 1 FROM news WHERE id='expired'").fetchone())

    def test_hourly_schedule_and_calendar_day_change(self):
        with patch('src.integration.service.time.monotonic',return_value=10000):
            first=self.service._cleanup('2026-10-25')
            self.assertIn('counts',first)
            self.assertEqual(self.service._cleanup('2026-10-25')['status'],'not_due')
            self.assertIn('counts',self.service._cleanup('2026-10-26'))

    def test_parser_boundary_invalid_dates_backup_and_busy_lock(self):
        db=self.fixture.root/'parser-news.db'
        with closing(sqlite3.connect(db)) as c:
            with c:
                c.execute('CREATE TABLE news(id TEXT PRIMARY KEY,published_utc TEXT)')
                c.executemany('INSERT INTO news VALUES(?,?)',[('old','2026-10-14T23:59:59Z'),('boundary','2026-10-15T00:00:00Z'),('unknown',None),('invalid','broken')])
        with take_storage_lock(db.parent/'parser.lock'):
            self.assertEqual(cleanup_parser(db,'2026-10-15').get('deferred'),'parser_busy')
        dry=cleanup_parser(db,'2026-10-15',dry_run=True)
        self.assertEqual(dry['deleted'],1);self.assertIsNone(dry['backup'])
        cleaned=cleanup_parser(db,'2026-10-15')
        self.assertEqual(cleaned['source_ids'],['old']);self.assertTrue(Path(cleaned['backup']).is_file())
        with closing(sqlite3.connect(db)) as c:self.assertEqual({r[0] for r in c.execute('SELECT id FROM news')},{'boundary','unknown','invalid'})
        self.assertNotIn('2026-10-14',db.with_suffix('.csv').read_text(encoding='utf-8-sig'))
        self.assertTrue(db.with_suffix('.csv').read_text(encoding='utf-8-sig').startswith('id;published_utc'))

    def test_expired_parser_job_without_main_news_is_removed(self):
        with connect(self.service.db) as c:
            with c:c.execute("INSERT INTO news_preparation_jobs VALUES('gone','hash','p','failed',3,NULL,'bad text','2026-10-14')")
        result=cleanup_news(self.service.db,10,'2026-10-25',expired_source_ids=['gone'])
        self.assertEqual(result['counts']['preparation_jobs'],1)
        with connect(self.service.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM news_preparation_jobs').fetchone()[0],0)

    def test_bad_retention_setting_is_rejected(self):
        for value in (0,-1,True,1.5):
            with self.assertRaises(ValueError):news_cutoff(value,'2026-10-25')


if __name__=='__main__':unittest.main()
