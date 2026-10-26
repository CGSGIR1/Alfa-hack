"""New agent version and event metadata: grounded, incremental, no paid calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from news_analytics.common import dumps,write_json
from news_analytics.database import connect
from news_analytics.event_identity import normalize,content_hash
from news_analytics.importing import import_all
from src.integration.adapter import result_for_batch
from src.integration.report_context import with_catalog
from src.report.deep import generate,Tools
from src.report.package import build
from src.llm import LLMError
from src.integration.events import compare_event_features


QUOTE='Банк России установил официальный курс доллара на 22 октября 2026 года на уровне 81,97 рубля за доллар США.'


def identity():
    return dict(schema_version='news-event-identity-1.0',main_thesis=QUOTE,event_type='single_event',
        actors=['Банк России'],action='Установил официальный курс',object='USD/RUB',stage='effective',
        event_date='2026-10-22',date_role='effective',period=None,
        metrics=[dict(name='Официальный курс',value='81.97',unit='рублей за доллар США',period=None,
                      source_block_id='B1',source_quote=QUOTE)],main_source_block_ids=['B1'],
        source_evidence=[dict(block_id='B1',quote=QUOTE)],limitations=[])


class DeepClient(fixture.FakeClient):
    def __init__(self,missing_item=False,invalid=False):
        super().__init__(invalid=invalid);self.missing_item=missing_item

    def chat_json(self,model,messages,task,params=None):
        if task.startswith('assess_'):
            out=super().chat_json(model,messages,'report_assess',params)
            self.tasks[-1]=task
            return out
        self.tasks.append(task);self.usage['calls']+=1
        data=json.loads(next(m['content'] for m in messages if m['role']=='user').split('\n',1)[1]) if task.startswith('analyst_') else None
        if task.startswith('analyst_'):
            if self.missing_item:return {'action':'final'}
            n=data['news']
            item=dict(news_id=n['id'],headline=n['title'],short_title=n['title'],topic='Издержки и цены',
                brief=dict(takeaway='При сохранении спроса возможно изменение затрат.',recommendation='Стоит оценить условия закупок.'),
                label='conditional',why_you_see_this='Связь через торговую деятельность.',
                what_happened=[dict(text=n['fragments'][0]['thesis'],type='news_fact',refs=[n['fragments'][0]['id']],company_basis=[])],
                why_it_matters=[],risks=[],opportunities=[],bank_view=[],not_established=['Устойчивость спроса.'],to_verify=[],next_steps=[],
                impact=dict(direction='risk',horizon='0-3m',confidence='medium',explanation='При сохранении спроса возможно изменение затрат.',rubric={
                    k:dict(score=v,note='Условная связь через торговую деятельность.') for k,v in
                    [('exposure',1),('magnitude',2),('certainty',2),('transmission',1),('mitigation',1)]}))
            return dict(action='final',item=item)
        if task=='synth':return dict(summary='При сохранении спроса возможен рост затрат компании.',ranking=[n['id'] for n in self.view['news']],independent_risks=[])
        if task=='critic':return dict(issues=[])
        raise AssertionError(task)


class DeepAgentTests(unittest.TestCase):
    def test_v2_has_short_summary_computed_impact_and_valid_outbox_ack(self):
        p=fixture.packet();client=DeepClient()
        doc,trace=generate(p,dict(full_texts={},candidates={}),client=client)
        self.assertEqual(doc['schema_version'],'company-report-2.0')
        self.assertEqual(doc['items'][0]['impact']['score'],7)
        self.assertEqual(doc['items'][0]['impact']['level'],'medium')
        self.assertLess(len(doc['summary']),500)
        result=result_for_batch(p,doc)
        self.assertEqual(result['items'][0]['applicability'],'conditional')
        self.assertEqual(set(client.tasks),{'assess_main','assess_second','analyst_N1_r0','synth','critic'})

    def test_incomplete_analysis_is_not_acknowledged_as_a_finished_report(self):
        with self.assertRaises(LLMError):
            generate(fixture.packet(),dict(full_texts={},candidates={}),client=DeepClient(missing_item=True))
        with self.assertRaises(LLMError):
            generate(fixture.packet(),dict(full_texts={},candidates={}),client=DeepClient(invalid=True))

    def test_main_service_uses_new_agents_and_second_run_is_idle(self):
        f=fixture.IntegratedWorkflowTests();f.setUp()
        try:
            f.service.config['pipeline']['report_mode']='deep'
            client=DeepClient()
            first=f.service.generate('C1','2026-10-23',client=client)
            self.assertEqual(first['status'],'completed')
            self.assertEqual(first['result']['narrative']['schema_version'],'company-report-2.0')
            calls=len(client.tasks)
            self.assertEqual(f.service.generate('C1','2026-10-23',client=client)['status'],'idle')
            self.assertEqual(len(client.tasks),calls)
        finally:f.tearDown()

    def test_partial_revision_receives_the_cited_analytical_sources(self):
        case=self
        class RevisionClient(DeepClient):
            def chat_json(self,model,messages,task,params=None):
                if task=='critic':
                    self.tasks.append(task)
                    return dict(issues=[dict(location='items[0].bank_view',type='clarity',problem='Проверить формулировку',suggestion='Сохранить условия')])
                if task.startswith('revise_items_'):
                    self.tasks.append(task)
                    payload=messages[-1]['content']
                    case.assertIn('A1::F1',payload)
                    case.assertIn('При росте затрат',payload)
                    part=json.JSONDecoder().raw_decode(payload.split('Раздел:\n',1)[1])[0]
                    return dict(part=part)
                result=super().chat_json(model,messages,task,params)
                if task.startswith('analyst_'):
                    result['item']['bank_view']=[dict(text='При росте затрат компания может изменить цены при устойчивом спросе.',
                        type='third_party_view',refs=['A1::M1'],company_basis=[])]
                return result
        doc,trace=generate(fixture.packet(),dict(full_texts={},candidates={}),client=RevisionClient())
        self.assertFalse(doc['quality']['report_issues_remaining'])

    def test_extra_evidence_is_preserved_and_bank_flag_requires_organizer(self):
        p=fixture.packet();view,index=build(p)
        m=deepcopy(p['analytical_mechanisms'][0]);m.update(id='A2::M1',document_id='A2',source_fragment_ids=['A2::F1'])
        s=deepcopy(p['analytical_source_fragments'][0]);s.update(id='A2::F1',document_id='A2')
        d=deepcopy(p['analytical_documents'][0]);d.update(id='A2',source_provenance={'provided_by_organizer':False})
        tool=Tools(dict(full_texts={},candidates={'N1':[dict(mechanism=m,document=d,sources=[s],news_fragment_id='N1::F1')]}),index)
        self.assertEqual(tool.run('more_analytics',{'news_id':'N1'})[0]['source_kind'],'third_party')
        self.assertIsInstance(tool.run('full_text',{'news_id':'OTHER'}),str)
        doc=dict(evidence_catalog=dict(analytical_documents=[d],analytical_mechanisms=[m],analytical_source_fragments=[s]))
        self.assertIn('A2',{d['id'] for d in with_catalog(p,doc)['analytical_documents']})

    def test_indicator_tool_reads_main_database_and_excludes_future_points(self):
        f=fixture.IntegratedWorkflowTests();f.setUp()
        try:
            with connect(f.service.db) as c:
                with c:c.executemany('INSERT INTO indicators VALUES(?,?,?,?,?)',[
                    ('fx_USD','2026-10-22',81.97,'Тест','2026-10-24'),
                    ('fx_USD','2026-10-24',80.53,'Тест','2026-10-24')])
            view,index=build(fixture.packet())
            tool=Tools(dict(full_texts={},candidates={},indicator_database=str(f.service.db)),index)
            self.assertEqual(tool.run('indicator_history',dict(code='fx_USD',days=60)),[('2026-10-22',81.97)])
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
