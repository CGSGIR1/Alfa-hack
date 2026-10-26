"""Final report/UI contract, source grounding and legacy compatibility. No API calls."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from test_agent_updates import DeepClient
from src.report.deep import generate,front_fields
from src.report.impact import compute
from src.report.package import build
from src.report.verify import check_report
from src.integration.site import feed,independent_context


class FinalReportTests(unittest.TestCase):
    def document(self):
        p=fixture.packet()
        doc,_=generate(p,dict(full_texts={},candidates={}),client=DeepClient())
        return p,doc

    def check(self,p,doc):
        return check_report(doc,build(p)[1],doc['assessment'],p['company']['facts'],p['company']['profile'],{},require_front_fields=True)

    def test_compact_fields_preserve_source_metadata_and_original_evidence(self):
        p,doc=self.document();item=doc['items'][0]
        self.assertEqual(self.check(p,doc),[])
        self.assertEqual(item['takeaway'],item['brief']['takeaway'])
        self.assertEqual(item['next_steps'][0],item['brief']['recommendation'])
        self.assertEqual(item['source_url'],p['news'][0]['source']['url'])
        self.assertEqual(item['published_at'],p['news'][0]['publication_date'])
        self.assertEqual(item['what_happened'][0]['refs'],['N1::F1'])
        self.assertEqual(item['topic'],'Издержки и цены')

    def test_unsupported_numbers_in_brief_and_figures_are_rejected(self):
        p,doc=self.document()
        doc['items'][0]['brief']['takeaway']='При сохранении спроса затраты могут вырасти на 123,45%.'
        doc['items'][0]['key_figures']=[dict(value='123,45%',label='Изменение затрат',refs=['N1::F1'])]
        issues=self.check(p,doc)
        self.assertTrue(any('brief.takeaway' in x and 'числа' in x for x in issues))
        self.assertTrue(any('key_figures' in x for x in issues))
        self.assertEqual(doc['items'][0]['key_figures'],[])

    def test_malformed_compact_fields_produce_validation_issues(self):
        p,doc=self.document()
        doc['items'][0]['brief']['takeaway']=123
        self.assertTrue(any('нет краткого блока' in x for x in self.check(p,doc)))
        front_fields(doc,build(p)[1])
        self.assertIsNone(doc['items'][0]['takeaway'])

    def test_conditional_brief_and_recommendations_keep_limits(self):
        p,doc=self.document()
        item=doc['items'][0]
        item['brief']['takeaway']='Затраты компании выросли.'
        item['brief']['recommendation']='Продать клиенту кредит.'
        issues=self.check(p,doc)
        self.assertTrue(any('без условности' in x for x in issues))
        self.assertTrue(any('рекомендац' in x or '[тон]' in x for x in issues))

    def test_rubric_cap_is_consistent_with_displayed_score(self):
        raw=dict(explanation='При выполнении условий возможен рост затрат.',confidence='high',
            rubric={k:dict(score=v,note='Условная связь.') for k,v in
                    [('exposure',1),('magnitude',4),('certainty',2),('transmission',2),('mitigation',2)]})
        impact,issues=compute(raw,'conditional')
        self.assertEqual(issues,[])
        self.assertEqual((impact['raw_score'],impact['score'],impact['level']),(11,8,'medium'))
        self.assertEqual(impact['confidence'],'medium')
        raw['rubric']['exposure']['score']=True
        self.assertTrue(compute(raw,'conditional')[1])

    def test_independent_context_has_sources_but_no_fake_news(self):
        p=fixture.packet()
        risk=dict(mechanism_id='A1::M1',headline='Условный перенос затрат',brief=dict(takeaway='При росте затрат возможен перенос.',recommendation='Стоит оценить спрос.'))
        records=independent_context(dict(as_of=p['as_of'],independent_risks=[risk]),p,dict(run_id='R1',completed_at='2026-10-23T10:00:00Z'))
        self.assertTrue(records[0]['independent'])
        self.assertEqual(records[0]['source'],{})
        self.assertEqual(records[0]['analytical_source_fragments'],p['analytical_source_fragments'])
        self.assertEqual(records[0]['connections'],[])

    def test_feed_carries_compact_document_and_never_mutates_saved_report(self):
        f=fixture.IntegratedWorkflowTests();f.setUp()
        try:
            f.service.config['pipeline']['report_mode']='deep'
            f.service.generate('C1','2026-10-23',client=DeepClient())
            result=feed(f.service.db,'C1',7,'2026-10-23')
            self.assertIn('brief',result['news'][0]['narrative'])
            self.assertEqual(result['latest_report']['summary'],'При сохранении спроса возможен рост затрат компании.')
            self.assertEqual(result['independent_risks'],[])
            self.assertEqual(feed(f.service.db,'C1',7,'2026-10-29')['latest_report'],result['latest_report'])
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
