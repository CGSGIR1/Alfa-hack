"""Regressions for the live conditional/rubric failures; no paid API calls."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'company-news-analytics'))
import test_integration as fixture
from test_agent_updates import DeepClient
from src.report.deep import generate
from src.report.errors import ReportValidationError
from src.report.package import build
from src.report.verify import check_report
from src.integration.admission import admits, policy
from src.integration.settings import load


class RepairClient(DeepClient):
    def __init__(self, persistent=False, missing_rubric=False):
        super().__init__()
        self.repairs=0
        self.persistent=persistent
        self.missing_rubric=missing_rubric

    def chat_json(self,model,messages,task,params=None):
        if task.startswith('revise_items_'):
            self.tasks.append(task);self.usage['calls']+=1;self.repairs+=1
            part=json.JSONDecoder().raw_decode(messages[-1]['content'].split('Раздел:\n',1)[1])[0]
            if self.repairs >= 2 and not self.persistent:
                part['why_it_matters'][0]['text']='При сохранении спроса издержки компании могут вырасти.'
                for factor in part['impact']['rubric'].values():
                    factor['note']='Условная связь через торговую деятельность.'
            elif self.missing_rubric:
                part['why_it_matters'][0]['text']='При сохранении спроса издержки компании могут вырасти.'
                part['impact']['rubric']={k:dict(score=v['score']) for k,v in part['impact']['rubric'].items()}
            return dict(part=part)
        result=super().chat_json(model,messages,task,params)
        if task.startswith('analyst_'):
            result['item']['why_it_matters']=[dict(text='Издержки компании вырастут.',type='ai_inference',
                refs=['N1::F1'],company_basis=['/facts/industry/name'])]
        return result


class ReportRepairTests(unittest.TestCase):
    def test_second_repair_can_finish_the_same_packet_without_losing_checks(self):
        client=RepairClient()
        doc,trace=generate(fixture.packet(),dict(full_texts={},candidates={}),client=client)
        self.assertEqual(client.repairs,2)
        self.assertFalse(doc['quality']['report_issues_remaining'])
        self.assertIn('F_second_revision',trace['steps'])
        self.assertIn('могут',doc['items'][0]['why_it_matters'][0]['text'])

    def test_a_second_failed_repair_is_terminal_instead_of_looping(self):
        client=RepairClient(persistent=True)
        with self.assertRaises(ReportValidationError):
            generate(fixture.packet(),dict(full_texts={},candidates={}),client=client)
        self.assertEqual(client.repairs,2)

    def test_missing_rubric_notes_are_repaired_by_the_model_not_fabricated(self):
        client=RepairClient(missing_rubric=True)
        doc,_=generate(fixture.packet(),dict(full_texts={},candidates={}),client=client)
        self.assertEqual(client.repairs,2)
        self.assertTrue(all(f['note'] for f in doc['items'][0]['impact']['rubric'].values()))

    def test_condition_elsewhere_does_not_justify_a_categorical_claim(self):
        packet=fixture.packet()
        doc,_=generate(packet,dict(full_texts={},candidates={}),client=DeepClient())
        doc['items'][0]['not_established']=['Если спрос сохранится, перенос затрат возможен.']
        doc['items'][0]['why_it_matters']=[dict(text='Издержки компании вырастут.',type='ai_inference',
            refs=['N1::F1'],company_basis=['/facts/industry/name'])]
        issues=check_report(deepcopy(doc),build(packet)[1],doc['assessment'],packet['company']['facts'],
                            packet['company']['profile'],require_front_fields=True)
        self.assertTrue(any('why_it_matters[0]' in issue and 'без условности' in issue for issue in issues))

    def test_company_news_threshold_is_0345_including_profile_hypotheses(self):
        options=policy(load())
        self.assertEqual(options,dict(minimum=.345,weak_minimum=.345))
        for evidence in ('grounded_topic','hypothesis','partial_payment_evidence'):
            for score,expected in ((.34499,False),(.345,True),(.36,True)):
                candidate=dict(company_score=score,matching_queries=[dict(weighted_score=score,
                    evidence=dict(evidence_status=evidence))])
                self.assertEqual(admits(candidate,**options),expected,(score,evidence))


if __name__=='__main__':unittest.main()
