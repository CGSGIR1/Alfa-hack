import copy,json,unittest
import numpy as np
from news_analytics.retrieval import select_company_news,Settings
from news_analytics.company_context import counterparty_summary


class PacketContextTests(unittest.TestCase):
    def data(self):
        queries=[];inputs={};vectors={}
        for i,(category,text,status) in enumerate([
            ('industry','Оптовая торговля продуктами','grounded_topic'),
            ('finance','Ключевая ставка и условия кредитования','grounded_topic'),
            ('macro','Изменение валютных курсов и трансграничные расчёты','hypothesis')]):
            queries.append(dict(company_id='C',is_active=1,weight=1,query_index=i,query_id='Q'+str(i),input_id='q'+str(i),channel=category,
                metadata_json=json.dumps(dict(category=category,evidence_status=status))))
            inputs['q'+str(i)]={'text':text};vectors['q'+str(i)]=np.eye(3)[i]
        news=[];chunks=[]
        for i,(title,vector) in enumerate([
            ('Продукты дорожают',[.95,.1,.1]),
            ('Ключевая ставка сохранена',[.2,.7,.1]),
            ('Цена рыбы указана в долларах',[.8,.1,.1]),
            ('Валютный курс рубля изменился',[.1,.1,.6])]):
            news.append(dict(id=str(i),title=title,url='https://example.org/'+str(i),publisher='Test',publication_date='2026-10-22',source_json='{}'))
            chunks.append(dict(id='f'+str(i),news_id=str(i),is_active=1,chunk_index=1,local_id='F',context_input_id='n'+str(i),
                metadata_json=json.dumps(dict(thesis=title,context='Контекст',title=title))))
            vectors['n'+str(i)]=np.array(vector)
        return dict(companies=[{'id':'C'}],company_queries=queries,inputs=inputs,vectors=vectors,news=news,article_chunks=chunks)
    def test_topic_coverage_preserves_base_selection_and_adds_qualified_events(self):
        data=self.data();base=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=0))
        rows,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=1,financial_news_limit=2),True)
        self.assertEqual(rows[0]['news_id'],base[0]['news_id'])
        self.assertEqual({r['news_id'] for r in rows},{'0','1','3'})
        self.assertEqual(rows[-1]['financial_topic_matches'][0]['evidence_status'],'hypothesis')
        self.assertTrue(all(t['status']=='added_for_topic_coverage' for t in coverage['topics']))
    def test_no_fake_fx_event_or_future_event_and_low_cosine_macro_is_explicit(self):
        data=self.data();data['news'][-1]['publication_date']='2026-10-23'
        data['vectors']['n1']=np.array([.1,.2,.1])
        rows,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=1),True)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]['selection_reason'],'financial_topic_coverage')
        self.assertTrue(rows[-1]['financial_topic_matches'][0]['below_similarity_threshold'])
        self.assertEqual(coverage['topics'][-1]['status'],'no_qualified_fresh_event')
        # Disabling coverage still applies the original score threshold.
        base=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=0))
        self.assertEqual(len(base),1)
    def test_incidental_rate_mention_in_company_bond_thesis_is_not_macro_event(self):
        data=self.data();data['news'][1]['title']='Компания разместила флоатер'
        data['article_chunks'][1]['metadata_json']=json.dumps(dict(thesis='Купон привязан к ключевой ставке.',context='Облигации',title='Флоатер'))
        _,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=1),True)
        self.assertEqual(coverage['topics'][0]['status'],'no_qualified_fresh_event')
    def test_hypothetical_financing_does_not_create_mandatory_slot(self):
        data=self.data();data['company_queries'][1]['metadata_json']=json.dumps(dict(category='finance',evidence_status='hypothesis'))
        _,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=1,financial_topic_coverage=1),True)
        self.assertEqual([t['topic'] for t in coverage['topics']],['currency'])
    def test_default_selection_has_no_mandatory_financial_slot_or_financial_cap(self):
        data=self.data()
        rows,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=4),True)
        self.assertEqual([r['news_id'] for r in rows],['0','2','1','3'])
        self.assertTrue(all(r['selection_reason']=='company_similarity' for r in rows))
        self.assertFalse(coverage['enabled']);self.assertEqual(coverage['max_extra_news'],0)
        self.assertIsNone(coverage['financial_news_limit'])
        limited=select_company_news(data,'C',Settings('2026-10-22',top_news=1))
        self.assertEqual([r['news_id'] for r in limited],['0'])
    def test_low_score_macro_events_are_rejected_like_other_topics(self):
        data=self.data()
        data['vectors']['n1']=np.array([.1,.2,.1]);data['vectors']['n3']=np.array([.1,.1,.2])
        rows,coverage=select_company_news(data,'C',Settings('2026-10-22',top_news=4),True)
        self.assertEqual([r['news_id'] for r in rows],['0','2'])
        self.assertEqual(coverage['topics'],[])
    def test_counterparty_projection_preserves_unknowns_denominators_and_group_scope(self):
        direction=dict(source_rows=10,unique_counterparties=10,pay_amt_total='100',pay_cnt_total=10,
            aggregates={'okved_division':[dict(key='(unknown)',share_amount_pct=70),dict(key='56',share_amount_pct=3,share_amount_among_known_pct=10)]},
            coverage={'okved_division':dict(unknown_amount_pct=70,matched_amount_pct=30)})
        profile={'relationships':{'payments':{'currency':None,'amount_share_denominator':'all rows of the direction',
            'same_group_rule':'Exclude known group only','summaries':{'excluding_same_group':{'incoming':direction}}}}}
        before=copy.deepcopy(profile);p=counterparty_summary(profile)
        d=p['selections']['excluding_same_group']['incoming']
        self.assertEqual(d['top_industries'][0]['share_amount_pct'],3)
        self.assertEqual(d['coverage']['unknown_amount_pct'],70);self.assertIsNone(p['currency'])
        self.assertEqual(profile,before);self.assertFalse(counterparty_summary({})['available'])


if __name__=='__main__':unittest.main()
