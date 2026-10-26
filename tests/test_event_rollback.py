"""A normal startup/import must not re-enable the archived event experiment."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.database import connect
from news_analytics.importing import import_all
from news_analytics.common import write_json
from src.news_prep.pipeline import PROMPT_VERSION,SYSTEM_PROMPT


class EventRollbackTests(unittest.TestCase):
    def test_news_preparation_uses_the_previous_prompt(self):
        self.assertEqual(PROMPT_VERSION,'news-two-views-1.0/p2')
        self.assertNotIn('event_identity',SYSTEM_PROMPT)

    def test_old_enriched_copy_cannot_reactivate_metadata_or_paid_work(self):
        f=fixture.IntegratedWorkflowTests();f.setUp()
        try:
            f.service.generate('C1','2026-10-23',client=fixture.FakeClient())
            with connect(f.service.db,True) as c:
                tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn('news_event_identities',tables)
                original=dict(c.execute('SELECT * FROM news').fetchone())
                vectors=[tuple(r) for r in c.execute('SELECT id,embedding FROM embedding_inputs ORDER BY id')]
                checks=[tuple(r) for r in c.execute('SELECT * FROM company_news_checks')]
            doc=json.loads(original['source_json'])
            doc['event_identity']=dict(schema_version='news-event-identity-1.0',main_thesis='Archived experiment')
            target=f.root/'archived-copy.json';write_json(target,doc)
            result=import_all(f.service.db,{'news':target},update_existing=True)
            self.assertEqual(result['changed_documents'],0)
            with connect(f.service.db,True) as c:
                current=dict(c.execute('SELECT * FROM news').fetchone())
                self.assertNotIn('event_identity',json.loads(current['source_json']))
                self.assertEqual(current['input_hash'],original['input_hash'])
                self.assertEqual(vectors,[tuple(r) for r in c.execute('SELECT id,embedding FROM embedding_inputs ORDER BY id')])
                self.assertEqual(checks,[tuple(r) for r in c.execute('SELECT * FROM company_news_checks')])
            self.assertEqual(f.service.generate('C1','2026-10-23',client=fixture.FakeClient())['status'],'idle')
            self.assertEqual(f.service.status()['counts']['news'],1)
        finally:f.tearDown()
