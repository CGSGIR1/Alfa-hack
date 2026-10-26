"""Onboarding boundaries: isolated SQLite/CSV, mocked inference, no paid calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'company-news-analytics'))
from news_analytics.common import write_json
from news_analytics.database import connect
from news_analytics.embedding import embed
from src.integration.onboarding import company_id
from src.integration.service import Service
from src.integration.settings import load
from src.integration.web import Application


def document(identifier='U_123'):
    return dict(schema_version='company-profile-2.0', subject_id=identifier, subject_type='company',
                profile_text='Тестовая торговая компания.', facts={'industry': {'main_okved_code':'46'}},
                queries=[dict(id=identifier+'_Q1', channel='supply', text='Рост стоимости поставок для торговли', weight=1)])


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load()
        self.cfg.update(database=self.root/'app.db', reports=self.root/'reports',
                        parser_directory=self.root/'parser', prepared_news=self.root/'prepared')
        self.cfg['inputs'] = {}
        self.cfg['upload_folders'] = {'companies':self.root/'uploads'}
        self.service = Service(self.cfg)
        self.identity = patch.object(self.service, 'model', return_value={})
        self.identity.start()
        self.embedding = patch('src.integration.service.embed', return_value={'computed':1})
        self.embed = self.embedding.start()

    def tearDown(self):
        self.embedding.stop(); self.identity.stop(); self.temp.cleanup()

    def test_id_normalization_and_paths_rejected(self):
        self.assertEqual(company_id(' 123 '), 'U_123')
        self.assertEqual(company_id('u_123'), 'U_123')
        for value in ('../U_123', 'U_1/../2', '', None, 123, 'group_123', '1;2'):
            with self.assertRaises(ValueError): company_id(value)

    def test_new_company_saved_once_and_embedding_retry_never_repeats_llm(self):
        with patch('src.integration.onboarding.build_profile', return_value=document()) as build:
            self.embed.side_effect = [RuntimeError('model unavailable'), {'computed':1}]
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                self.service.add_company('123')
            result = self.service.add_company('U_123')
            self.assertEqual(build.call_count, 1)
        self.assertEqual(result['source'], 'database')
        self.assertEqual(self.embed.call_args.kwargs['company_id'], 'U_123')
        with connect(self.service.db, True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM companies').fetchone()[0], 1)
            row = c.execute('SELECT input_file,source_json FROM companies').fetchone()
            self.assertEqual(json.loads(Path(row['input_file']).read_text())['subject_id'], 'U_123')
            self.assertEqual(json.loads(row['source_json'])['facts']['industry']['main_okved_code'], '46')

    def test_prepared_json_without_csv_or_llm(self):
        self.cfg['inputs'] = {'companies':self.root/'inputs'}
        write_json(self.root/'inputs/company.json', document())
        with patch('src.integration.onboarding.build_profile', side_effect=AssertionError('No paid calls')):
            self.assertEqual(self.service.add_company('123')['source'], 'json')

    def test_invalid_llm_json_does_not_install_anything(self):
        bad = document(); bad['queries'] = []
        with patch('src.integration.onboarding.build_profile', return_value=bad):
            with self.assertRaises(ValueError): self.service.add_company('123')
        self.embed.assert_not_called()
        with connect(self.service.db, True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM companies').fetchone()[0], 0)
        self.assertFalse(list(self.root.rglob('uploaded-*.json')))

    def test_llm_failure_does_not_install_or_select_company(self):
        with patch('src.integration.onboarding.build_profile', side_effect=ValueError('LLM недоступна')):
            with self.assertRaisesRegex(ValueError, 'LLM'): self.service.add_company('123')
        self.assertIsNone(self.service.selected_company())
        self.embed.assert_not_called()

    def test_csv_uses_facts_and_requires_successful_llm_refinement(self):
        import pandas as pd
        from src.company_profile.data import FLAG_COLUMNS
        from src.company_profile.queries import QuerySet
        from src.integration.profiles import company_json
        from src.integration.onboarding import build_profile
        base=dict(MAINOKVED_CODE='46.38',MAINOKVED_NAME='Оптовая торговля',GROUP_ID='group_test',
            FTS_COUNTRY_NAME='КИТАЙ',REGION='Регион',CITY='Город',OKFS_NAME='Частная собственность',
            COMPANY_AGE_YEARS_GRADATION='5-10',AMOUNT_WORKERS_FNS_GRADATION='10-50',
            AMOUNT_WORKERS_SPARK_GRADATION='10-50',ECO_MARK_LABEL='Развитие',CL_TYPE='Клиент',ORG_TYPE='ЮЛ',
            **{k:0 for k in FLAG_COLUMNS})
        pd.DataFrame([dict(base,SYNTHETIC_U='U_123',REVENUE_COMPANY=1000000)]).to_csv(
            self.root/'df_Uid_data_hack.csv',sep=';',index=False)
        pd.DataFrame(columns=['PAYER','BENEFICIARY','PAY_AMT','PAY_CNT']).to_csv(self.root/'df_Uid_payment_hack.csv',sep=';',index=False)
        pd.DataFrame(columns=['SYNTHETIC_U','SYNTHETIC_F','CITY_NAME','RC_NAME']).to_csv(self.root/'df_Fid_Uid_link_hack.csv',sep=';',index=False)
        with patch.dict(os.environ, {'DATA_DIR':str(self.root)}), patch('src.company_profile.impact.get_impact_queries') as refine:
            with self.assertRaisesRegex(ValueError, 'не найдена'): build_profile(self.cfg, 'U_999')
            refine.assert_not_called()
            rules=company_json('U_123',self.root)
            from src.company_profile.queries import ImpactQuery
            refine.return_value=QuerySet(subject_id='U_123',subject_type='company',queries=[ImpactQuery(
                channel='supply',text='Стоимость поставок для оптовой торговли',weight=1,kind='llm',origin='llm',
                category='industry',why='Из отрасли CSV',sources=[])],notes=[])
            result=build_profile(self.cfg, 'U_123')
            self.assertEqual(result['facts'], rules['facts'])
            self.assertEqual(result['queries'][0]['provenance']['origin'], 'llm')
            refine.return_value.notes=['LLM недоступна (test), используются запросы из правил']
            with self.assertRaisesRegex(ValueError, 'LLM'): build_profile(self.cfg, 'U_123')

    def test_embedding_scope_does_not_process_unrelated_news(self):
        from news_analytics.importing import import_all
        write_json(self.root/'one.json', document())
        other=document('U_456'); other['queries'][0]['text']='Другие отраслевые события'
        write_json(self.root/'two.json', other)
        import_all(self.service.db, {'companies':self.root})
        with patch('news_analytics.embedding.vector', return_value=(object(),None)):
            stats=embed(self.service.db,self.root,model={'unused':True},company_id='U_123')
        self.assertEqual(stats['total'],1)
        self.assertEqual(stats['reused'],1)

    def test_background_tick_yields_before_starting_another_paid_report(self):
        with patch.object(self.service,'_generate') as generate, patch.object(self.service,'_embed',return_value={}):
            result=self.service.tick(parse=False,prepare=False,continuous=True,should_continue=lambda:False)
        generate.assert_not_called()
        self.assertEqual(result['results'],[])

    def test_web_job_queues_behind_report_and_deduplicates_double_click(self):
        config_file=self.root/'config.json'
        serial=json.loads((ROOT/'config.json').read_text())
        serial.update({k:str(self.cfg[k]) for k in ('database','reports','parser_directory','prepared_news','model_path')})
        serial['inputs']={};serial['upload_folders']={'companies':str(self.root/'uploads')}
        write_json(config_file,serial)
        app=Application(config_file,self.cfg)
        release=threading.Event(); started=threading.Event(); order=[]
        def report():
            started.set();release.wait(5);order.append('report')
            app._update('report',status='complete')
        app.jobs['report']=dict(id='report',kind='generate',status='running');app.active='report'
        app.pool.submit(report);started.wait(2)
        try:
            with patch.object(app.service,'add_company',side_effect=lambda identifier,progress: (order.append(identifier) or {'company_id':identifier})):
                first=app.start('add-company',{'company_id':'123'})
                self.assertEqual(app.start('add-company',{'company_id':'U_123'}),first)
                second=app.start('add-company',{'company_id':'456'})
                self.assertIn('Ожидаем',app.get_job(first['job_id'])['phase'])
                self.assertTrue(app.profile_pending.is_set())
                with self.assertRaises(BlockingIOError): app.start('tick', {'automatic':True})
                release.set();app.pool.shutdown(wait=True)
            self.assertEqual(order,['report','U_123','U_456'])
            self.assertEqual(app.get_job(first['job_id'])['status'],'complete')
            self.assertEqual(app.get_job(second['job_id'])['status'],'complete')
            self.assertFalse(app.profile_pending.is_set())
        finally:
            release.set();app.pool.shutdown(wait=True)


if __name__ == '__main__': unittest.main()
