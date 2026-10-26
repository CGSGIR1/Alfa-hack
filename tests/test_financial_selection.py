"""Changing financial selection policy must not replay completed LLM work."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.workflow import history
from src.integration.settings import load,retrieval


class FinancialSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service

    def tearDown(self):self.fixture.tearDown()

    def test_pending_legacy_quota_batch_is_replaced_under_common_selection(self):
        self.service.config['retrieval']['financial_topic_coverage']=1
        before=self.service.compare('C1','2026-10-23')
        self.service.config['retrieval']['financial_topic_coverage']=0
        after=self.service.compare('C1','2026-10-23')
        self.assertNotEqual(before['saved']['run_id'],after['saved']['run_id'])
        self.assertFalse(after['report']['financial_topic_coverage']['enabled'])
        statuses={row['run_id']:row['status'] for row in history(self.service.db,'C1')}
        self.assertEqual(statuses[before['saved']['run_id']],'cancelled')
        self.assertEqual(statuses[after['saved']['run_id']],'prepared')

    def test_completed_news_are_not_sent_to_llm_again_after_policy_change(self):
        self.service.config['retrieval']['financial_topic_coverage']=1
        client=fixture.FakeClient()
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'completed')
        calls=len(client.tasks)
        self.service.config['retrieval']['financial_topic_coverage']=0
        self.assertEqual(self.service.generate('C1','2026-10-23',client=client)['status'],'idle')
        self.assertEqual(len(client.tasks),calls)
        self.assertEqual(retrieval(load(),'2026-10-23').financial_topic_coverage,0)


if __name__=='__main__':unittest.main()
