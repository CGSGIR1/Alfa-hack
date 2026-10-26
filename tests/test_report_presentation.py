"""Public report sources follow final citations; service reasoning stays internal."""
from copy import deepcopy
from pathlib import Path
import json,sys,unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.workflow import history
from src.integration.report_context import cited_analytics,with_catalog
from src.integration.site import feed


class PresentationTests(unittest.TestCase):
    def test_final_citation_can_come_from_extra_evidence_instead_of_candidates(self):
        packet=fixture.packet()
        original=deepcopy(packet['analytical_mechanisms'][0])
        mechanism=deepcopy(original);mechanism.update(id='A2::M1',document_id='A2',source_fragment_ids=['A2::F1'])
        mechanism['evidence_refs'][0]['fragment_id']='A2::F1'
        source=deepcopy(packet['analytical_source_fragments'][0]);source.update(id='A2::F1',document_id='A2')
        doc=deepcopy(packet['analytical_documents'][0]);doc['id']='A2'
        catalog=dict(analytical_documents=[doc],analytical_mechanisms=[mechanism],analytical_source_fragments=[source])
        full=with_catalog(packet,dict(evidence_catalog=catalog))
        prose=dict(bank_view=[dict(text='Итоговая оценка.',refs=['A2::F1'])])
        before=deepcopy(full)
        evidence=cited_analytics(prose,full)
        self.assertEqual(evidence,catalog)
        self.assertEqual(full,before)
        self.assertEqual(cited_analytics({},full)['analytical_mechanisms'],[])

    def test_public_feed_does_not_expose_arbiter_reason_or_uncited_candidates(self):
        f=fixture.IntegratedWorkflowTests();f.setUp()
        try:
            f.service.generate('C1','2026-10-23',client=fixture.FakeClient())
            rows=history(f.service.db,'C1');stored=deepcopy(rows)
            item=rows[0]['result']['items'][0]
            item['reason']='SERVICE_REASON opinion_1 /facts/industry'
            item['assessments'][0]['company_impact']['statement']='SERVICE_LINK opinion_2'
            item['narrative']['why_you_see_this']='Публичное пояснение для компании.'
            with patch('src.integration.site.history',return_value=rows):
                visible=feed(f.service.db,'C1',7,'2026-10-23')
            card=visible['news'][0]
            self.assertEqual(card['reason'],'Публичное пояснение для компании.')
            self.assertEqual(card['analytical_mechanisms'],[])
            self.assertEqual(card['assessments'],[])
            self.assertNotIn('SERVICE_',json.dumps(visible))
            self.assertEqual(history(f.service.db,'C1'),stored)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
