"""Do not spend LLM calls or news quotas on events lacking analytical support."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.database import connect
from news_analytics.common import write_json,dumps
from news_analytics.importing import import_all
from news_analytics.llm_io import report_input
from src.integration.site import feed


class AnalyticsRequirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp();self.service=self.fixture.service

    def tearDown(self):self.fixture.tearDown()

    def test_no_analytics_no_llm_and_news_can_be_reconsidered_later(self):
        with connect(self.service.db) as c:
            with c:c.execute("UPDATE analytics SET document_date='2026-10-24'")
        client=fixture.FakeClient()
        first=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual(first['status'],'idle');self.assertEqual(client.tasks,[])
        self.assertEqual(first['workflow']['without_analytics_skipped'],1)
        with connect(self.service.db,True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM company_report_batches').fetchone()[0],0)
            check=c.execute('SELECT disposition,assigned_run_id FROM company_news_checks').fetchone()
            self.assertEqual(tuple(check),('queued',None))
        with patch('news_analytics.workflow.score_company_news',side_effect=AssertionError('Company cosine recomputed')):
            self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
            with connect(self.service.db) as c:
                with c:c.execute("UPDATE analytics SET document_date='2026-09-01'")
            later=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual(later['status'],'completed');self.assertTrue(client.tasks)
        self.assertEqual(later['workflow']['new_comparisons'],0)

    def test_unsupported_top_news_does_not_use_a_news_slot(self):
        news=fixture.helpers.news();news['document_id']='N2';news['document_title']='Другое отраслевое событие';news['source']['url']='https://example.org/n2'
        target=self.fixture.root/'n2.json';write_json(target,news)
        import_all(self.service.db,{'news':target});self.fixture.fill_vectors(self.service.db)
        with connect(self.service.db) as c:
            with c:c.execute("UPDATE article_chunks SET mechanism_input_id=NULL WHERE news_id='N1'")
        result=self.service.compare('C1','2026-10-23',top_news=1)
        self.assertEqual(result['status'],'prepared')
        self.assertEqual([n['news_id'] for n in result['report']['news']],['N2'])
        self.assertEqual(result['workflow']['without_analytics_skipped'],1)
        self.assertEqual([n['id'] for n in report_input(result['report'])['news']],['N2'])

    def test_single_branch_candidate_is_not_enough_for_report_admission(self):
        import numpy as np
        unrelated=np.zeros(768,dtype='<f4');unrelated[5]=1
        with connect(self.service.db) as c:
            with c:c.execute("UPDATE embedding_inputs SET embedding=? WHERE role='analytics_source'",(unrelated.tobytes(),))
        client=fixture.FakeClient();result=self.service.generate('C1','2026-10-23',client=client)
        self.assertEqual(result['status'],'idle');self.assertEqual(client.tasks,[])
        self.assertEqual(result['workflow']['without_analytics_skipped'],1)

    def test_pending_old_batch_without_analytics_is_not_sent(self):
        old=self.service.compare('C1','2026-10-23')
        altered=deepcopy(old['report']);altered['news'][0]['analytics']=[]
        with connect(self.service.db) as c:
            with c:
                c.execute('UPDATE company_reports SET report_json=? WHERE run_id=? AND company_id=?',
                    (dumps(altered),old['saved']['run_id'],'C1'))
                c.execute("UPDATE analytics SET document_date='2026-10-24'")
        client=fixture.FakeClient();result=self.service.finish('C1',old['saved']['run_id'],client=client)
        self.assertEqual(result['status'],'idle');self.assertEqual(client.tasks,[])
        with connect(self.service.db,True) as c:
            self.assertEqual(c.execute('SELECT status FROM company_report_batches').fetchone()[0],'cancelled')
            self.assertIsNone(c.execute('SELECT assigned_run_id FROM company_news_checks').fetchone()[0])

    def test_packet_and_old_feed_hide_unsupported_news(self):
        completed=self.service.generate('C1','2026-10-23',client=fixture.FakeClient())
        altered=deepcopy(completed['report']);altered['news'][0]['analytics']=[]
        altered['financial_topic_coverage']=dict(topics=[dict(topic='currency',news_ids=['N1'])])
        packet=report_input(altered)
        self.assertEqual(packet['news'],[]);self.assertEqual(packet['connections'],[])
        self.assertEqual(packet['analytical_source_fragments'],[])
        self.assertEqual(packet['coverage']['financial_topics']['topics'][0]['news_ids'],[])
        with connect(self.service.db) as c:
            with c:c.execute('UPDATE company_reports SET report_json=? WHERE run_id=? AND company_id=?',
                (dumps(altered),completed['saved']['run_id'],'C1'))
        self.assertEqual(feed(self.service.db,'C1',as_of='2026-10-23')['news'],[])
