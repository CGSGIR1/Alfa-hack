"""Reference fidelity, feed scoping and live controls; no paid model calls."""
from pathlib import Path
from html.parser import HTMLParser
from copy import deepcopy
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'company-news-analytics'))
sys.path.insert(0,str(ROOT))
from src.integration.site import feed, company_payments
from src.integration.settings import save_auto_poll
import test_integration as integration_tests
from news_analytics.workflow import history,batch_report


class Markup(HTMLParser):
    def __init__(self):
        super().__init__();self.tokens=[];self.skip=None
    def handle_starttag(self,tag,attrs):
        if self.skip:return
        if tag in ('script','style'):self.skip=tag;return
        if tag=='link' and dict(attrs).get('href') in ('/style.css','/integration.css'):return
        if tag=='link' and dict(attrs).get('rel')=='icon':return
        normalized=[]
        for key,value in attrs:
            if key=='style':continue
            if key=='class':
                value=' '.join(v for v in value.split() if not v.startswith('reference-inline-'))
                if not value:continue
            normalized.append((key,value))
        self.tokens.append(('start',tag,sorted(normalized)))
    def handle_endtag(self,tag):
        if self.skip:
            if tag==self.skip:self.skip=None
            return
        self.tokens.append(('end',tag))
    def handle_data(self,text):
        if not self.skip and text.strip():self.tokens.append(('text',' '.join(text.split())))


class ReferenceTests(unittest.TestCase):
    def test_original_css_and_markup_are_preserved(self):
        source=(ROOT/'design/reference.html').read_bytes().decode('utf-8-sig')
        styles=re.findall(r'<style[^>]*>([\s\S]*?)</style>',source,re.I)
        self.assertEqual((ROOT/'src/integration/web_static/style.css').read_bytes(),styles[0].encode())
        # The approved redesign changes the top dashboard and company dialog.
        # Keep the original CSS and the stable control/icon contract, rather
        # than requiring the obsolete entire DOM to stay byte-for-byte equal.
        current_html=(ROOT/'src/integration/web_static/index.html').read_text(encoding='utf-8')
        reference,current=Markup(),Markup();reference.feed(source);current.feed(current_html)
        def ids(markup):
            return [dict(t[2])['id'] for t in markup.tokens if t[0]=='start' and 'id' in dict(t[2])]
        original_ids=set(ids(reference));current_ids=ids(current)
        removed={'conv-tag','dismissed','dismissed-list','dismissed-n','h-indep','h-missing',
                 'h-mkt','h-ops','indep','indep-list','missing','missing-list','mkt','mkt-body','ops','w-ops'}
        self.assertTrue((original_ids-removed).issubset(current_ids))
        self.assertEqual(len(current_ids),len(set(current_ids)), 'DOM IDs must be unique')
        for token in current.tokens:
            if token[0]=='start' and token[1]=='use':
                ref=dict(token[2]).get('href','')
                self.assertIn(ref.removeprefix('#'),current_ids)
        self.assertIn('data-theme="dark"',current_html)
        self.assertIn('id="add-company-dialog"',current_html)
        self.assertIn('id="home-stories"',current_html)
        self.assertIn('id="home-work-heading"',current_html)
        self.assertNotIn('RAW_NEWS',current_html)
        self.assertIn('/site-adapter.js',current_html)
        self.assertIn('rel="icon" href="/favicon.ico"',current_html)

    def test_auto_setting_preserves_other_env_values(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=False):
            target=Path(tmp)/'.env'
            target.write_text('LLM_API_KEY=test-only-sentinel\nPIPELINE_AUTO_POLL=0\nAPP_PORT=9999\n',encoding='utf-8')
            with patch('src.integration.settings.ACTIVE_ENV_FILE',target):
                save_auto_poll(True)
                self.assertIn('PIPELINE_AUTO_POLL=1',target.read_text())
                save_auto_poll(False)
            self.assertIn('LLM_API_KEY=test-only-sentinel',target.read_text())
            self.assertIn('APP_PORT=9999',target.read_text())
            self.assertIn('PIPELINE_AUTO_POLL=0',target.read_text())

    def test_payment_currency_is_not_invented_and_invalid_values_are_skipped(self):
        document={'subject_id':'C1','relationships':{'payments':{'rows':[
            dict(payer_id='C1',beneficiary_id='C2',amount_source_value='10.5',payment_count=2),
            dict(payer_id='C1',beneficiary_id='C2',amount_source_value='2.5',payment_count=1),
            dict(payer_id='C1',beneficiary_id='C3',amount_source_value='NaN',payment_count=1)]}}}
        result=company_payments(document)
        self.assertIsNone(result['currency'])
        self.assertEqual(len(result['outgoing']),1)
        self.assertEqual(result['outgoing'][0]['amount'],'13.0')
        self.assertEqual(result['outgoing'][0]['count'],3)


class SiteFeedTests(unittest.TestCase):
    def setUp(self):
        self.fixture=integration_tests.IntegratedWorkflowTests();self.fixture.setUp()
        self.service=self.fixture.service
    def tearDown(self):self.fixture.tearDown()

    def test_feed_is_read_only_and_keeps_completed_reports_indefinitely(self):
        self.service.generate('C1','2026-10-23',client=integration_tests.FakeClient())
        before=history(self.service.db,'C1')
        current=feed(self.service.db,'C1',7,'2026-10-23')
        self.assertEqual(len(current['news']),1)
        self.assertIn('source_blocks',current['news'][0])
        self.assertEqual(current['company']['id'],'C1')
        self.assertNotIn('source_path',json.dumps(current))
        self.assertEqual(len(feed(self.service.db,'C1',7,'2026-10-29')['news']),1)
        self.assertIsNone(current['report_retention_days'])
        self.assertEqual(current['feed_period_default'],'all')
        self.assertEqual(current['period_basis'],'delivery_date')
        self.assertEqual(current['window_days'],7)
        self.assertEqual(feed(self.service.db,'C1',7,'2026-10-01')['news'],[])
        self.assertEqual(history(self.service.db,'C1'),before)
        with self.assertRaises(ValueError):feed(self.service.db,'unknown')

    def test_later_rejection_does_not_resurrect_old_accepted_news(self):
        self.service.generate('C1','2026-10-23',client=integration_tests.FakeClient())
        old=history(self.service.db,'C1')[0]
        rejected=deepcopy(old);rejected['result']['items'][0]['applicability']='rejected'
        with patch('src.integration.site.history',return_value=[rejected,old]):
            self.assertEqual(feed(self.service.db,'C1',7,'2026-10-23')['news'],[])

    def test_later_report_without_independent_section_keeps_delivered_context(self):
        self.service.generate('C1','2026-10-23',client=integration_tests.FakeClient())
        older=history(self.service.db,'C1')[0]
        saved=batch_report(self.service.db,older['run_id'],'C1')
        older['completed_at']='2026-10-23T09:00:00+00:00'
        context=dict(mechanism_id='A1_F1_M1',headline='Условный сценарий компании',
            brief=dict(takeaway='При сохранении спроса возможен перенос затрат.',recommendation='Стоит оценить спрос.'))
        older['result']['narrative']['independent_risks']=[context]
        newer=deepcopy(older);newer.update(run_id='next-report',completed_at='2026-10-23T10:00:00+00:00')
        newer['result']['narrative']['independent_risks']=[]
        with patch('src.integration.site.batch_report',return_value=saved),patch('src.integration.site.history',return_value=[newer,older]):
            current=feed(self.service.db,'C1',7,'2026-10-23')
        self.assertEqual(len(current['news']),1)
        self.assertEqual(current['independent_risks_scope'],'history')
        self.assertEqual(len(current['independent_risks']),1)
        self.assertEqual(current['independent_risks'][0]['title'],context['headline'])
        self.assertEqual(current['independent_risks'][0]['run_id'],older['run_id'])
        # A new version of that same scenario updates the stable card once.
        newer['result']['narrative']['independent_risks']=[dict(context,headline='Уточнённый сценарий')]
        with patch('src.integration.site.batch_report',return_value=saved),patch('src.integration.site.history',return_value=[older,newer]):
            updated=feed(self.service.db,'C1',7,'2026-10-23')
        self.assertEqual(len(updated['independent_risks']),1)
        self.assertEqual(updated['independent_risks'][0]['title'],'Уточнённый сценарий')
        self.assertEqual(updated['independent_risks'][0]['feed_id'],current['independent_risks'][0]['feed_id'])

    def test_live_auto_toggle_requires_boolean_and_llm_configuration(self):
        from src.integration.web import Application
        from news_analytics.common import write_json
        config_file=self.fixture.root/'config.json'
        write_json(config_file,{k:str(v) if isinstance(v,Path) else v for k,v in self.fixture.cfg.items()})
        app=Application(config_file,self.fixture.cfg)
        try:
            with patch('src.integration.settings.save_auto_poll') as saved,patch('src.integration.settings.llm_ready',return_value=True):
                self.assertTrue(app.start('auto-poll',{'enabled':True})['auto_poll'])
                self.assertFalse(app.start('auto-poll',{'enabled':False})['auto_poll'])
                self.assertEqual(saved.call_count,2)
                self.assertIsNone(app.active)
                with self.assertRaises(ValueError):app.start('auto-poll',{'enabled':'false'})
            with patch('src.integration.settings.llm_ready',return_value=False):
                with self.assertRaises(ValueError):app.start('auto-poll',{'enabled':True})
        finally:app.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
