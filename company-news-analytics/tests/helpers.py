from news_analytics.common import sha

def company():return {'schema_version':'company-profile-2.0','subject_id':'C1','subject_type':'company','facts':{'industry':{'main_okved_name':'Торговля'}},
 'profile_text':'Компания занимается торговлей.','queries':[{'id':'sector','channel':'industry','text':'Торговая компания','weight':1}]}
def news():return {'schema_version':'news-two-views-1.0','document_id':'N1','document_title':'Рост затрат','document_date':'2026-10-22',
 'source':{'url':'https://example.org/news/1','publisher':'Test'},'fragments':[{'id':'F1','title':'Рост затрат','thesis':'Затраты выросли.',
 'context':'Торговля в России.','company_embedding_text':'Затраты выросли. Торговля в России.',
 'analytics_search_question':'Как рост затрат влияет на цены?','mechanism':{'description':'Рост затрат может влиять на цены.','status':'hypothesis',
 'boundaries':['Условный механизм'],'is_established_cause':False,'context':{'conditions':'Торговля, при возможности переноса затрат.'}},'limitations':[]}]}
def analytics():
    original='Рост затрат может приводить к росту цен, если спрос допускает перенос.'
    body='Причина: Рост затрат.\nКанал: Перенос в цены.\nСледствие: Рост цен.\nУсловия: При устойчивом спросе.'
    return {'schema_version':'analytics-contextual-mechanisms-2.0','document':{'id':'A1','title':'Затраты и цены','date':'2026-10-21',
     'period':None,'source_sha256':'a'*64,'page_count':1},'source_pages':[{'page':1,'text':original}],
     'charts':[],'tables':[],'chunks':[{'id':'A1_F1','document_id':'A1','local_id':'F1','title':'Затраты и цены','text':original,
     'embedding_text':original,'content_sha256':sha(original),'page':1,'pages':[1],'mechanism_ids':['A1_F1_M1']}],
     'mechanisms':[{'id':'A1_F1_M1','document_id':'A1','title':'Перенос затрат','mechanism':{'cause':'Рост затрат.','channel':'Перенос в цены.','effect':'Рост цен.'},
     'context':{'conditions':'При устойчивом спросе.'},'embedding_text':body,'content_sha256':sha(body),'encoding_method':'encode_document',
     'eligible_for_retrieval':True,'statement_type':'author_hypothesis','source_fragment_ids':['A1_F1'],'source_text':original,'source_pages':[1],
     'evidence_refs':[{'fragment_id':'A1_F1','quote':original,'pages':[1]}],'related_visual_ids':[],'limitations':['Перенос не гарантирован.']}]}
