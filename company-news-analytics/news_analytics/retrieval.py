"""Selected algorithm: the SAME news mechanism query searches TWO analytical indexes."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from collections import Counter,defaultdict
from datetime import date
import calendar,json,math,re
import numpy as np
from .common import require,sha
from .database import connect
from .embedding import vector
from .company_context import counterparty_summary
from .analytics_sources import source_provenance

@dataclass(frozen=True)
class Settings:
    as_of:str
    top_news:int=3
    news_fragments:int=2
    analytics_per_news:int=10
    max_chunks_per_analytics:int=2
    news_days:int=30
    max_analytics_age_days:int=365
    min_news_score:float=.35
    min_analytics_score:float=.375
    financial_topic_coverage:int=1
    financial_news_limit:int=1
    def validate(self):
        date.fromisoformat(self.as_of)
        for key in ('top_news','news_fragments','analytics_per_news','max_chunks_per_analytics','news_days','max_analytics_age_days','financial_news_limit'):
            require(type(getattr(self,key)) is int and getattr(self,key)>0,key+' must be a positive integer')
        for key in ('min_news_score','min_analytics_score'):
            v=getattr(self,key);require(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1,key+' must be in [0,1]')
        require(type(self.financial_topic_coverage) is int and self.financial_topic_coverage in (0,1),'financial_topic_coverage must be 0 or 1')
        return self

def available(doc):
    if doc['document_date']:return date.fromisoformat(doc['document_date']),'day'
    if doc['document_period']:
        y,m=map(int,doc['document_period'].split('-'));return date(y,m,calendar.monthrange(y,m)[1]),'month_end_bound'
    return None,'unknown'

def snapshot(db,model):
    with connect(db,True) as c:
        c.execute('BEGIN')
        data={table:[dict(r) for r in c.execute('SELECT * FROM '+table)] for table in
              ('companies','company_queries','news','article_chunks','analytics','analytics_chunks','analytics_mechanisms','analytics_mechanism_sources')}
        inputs=[dict(r) for r in c.execute('SELECT * FROM active_embedding_inputs')]
    vectors={};errors=[]
    for row in inputs:
        v,error=vector(row,model)
        if error:errors.append(dict(input_id=row['id'],role=row['role'],reason=error))
        else:vectors[row['id']]=v
    require(not errors,'Missing or incompatible active vectors; run embed first. '+str(errors[:10]))
    data['inputs']={r['id']:r for r in inputs};data['vectors']=vectors
    data['input_fingerprint']=sha('\n'.join(f"{r['role']}:{r['id']}:{r['text_hash']}:{r['model_revision']}" for r in sorted(inputs,key=lambda x:x['id']))+
        '\n'+'\n'.join(f"{table}:{r['id']}:{r['input_hash']}" for table in ('companies','news','analytics') for r in sorted(data[table],key=lambda x:x['id'])))
    return data

TOPIC_PATTERNS={
 'financing':re.compile(r'ключев\w*\s+ставк|услови\w*\s+(?:кредитован|лизинг)|стоимост\w*\s+(?:кредит|заимствован)|ставк\w*\s+по\s+кредит',re.I),
 'currency':re.compile(r'валютн\w*\s+курс|курс\w*\s+(?:рубл|доллар|евро|юан)|трансграничн\w*\s+(?:расч[её]т|плат[её]ж)|курсов\w*\s+(?:разниц|колеб)',re.I)}


def event_topic(title,topic):
    # Require the topic in the headline, not an incidental background mention
    # (e.g. an individual issuer's floating-rate bond is not a policy-rate event).
    if topic=='financing':
        return bool(re.search(r'ключев\w*\s+ставк|(?:ЦБ|Банк\w*\s+России).{0,80}ставк|ставк.{0,80}(?:ЦБ|Банк\w*\s+России)',title,re.I)) and not bool(re.search(r'вклад|депозит|максимальн',title,re.I))
    return bool(TOPIC_PATTERNS[topic].search(title))


def financial_queries(queries,inputs):
    result={}
    for q in queries:
        meta=json.loads(q['metadata_json']);text=inputs[q['input_id']]['text']
        if meta.get('category') not in ('finance','macro'):continue
        status=meta.get('evidence_status')
        if status not in ('grounded_topic','hypothesis'):continue
        for topic,pattern in TOPIC_PATTERNS.items():
            if pattern.search(text) and (topic!='financing' or status=='grounded_topic'):
                result.setdefault(topic,[]).append(q['query_id'])
    return result


def score_company_news(data,company_id,settings):
    companies={c['id']:c for c in data['companies']};require(company_id in companies,'Unknown company: '+company_id)
    queries=sorted([q for q in data['company_queries'] if q['company_id']==company_id and q['is_active'] and q['weight']>0],key=lambda q:(q['query_index'],q['query_id']))
    require(queries,'Company has no active positive-weight queries: '+company_id)
    today=date.fromisoformat(settings.as_of)
    news={n['id']:n for n in data['news'] if 0<=(today-date.fromisoformat(n['publication_date'])).days<settings.news_days}
    chunks=sorted([c for c in data['article_chunks'] if c['is_active'] and c['news_id'] in news],key=lambda c:(c['news_id'],c['chunk_index']))
    requested=financial_queries(queries,data['inputs']) if settings.financial_topic_coverage else {}
    if not chunks:return []
    vectors=data['vectors'];qmat=np.stack([vectors[q['input_id']] for q in queries]);nmat=np.stack([vectors[c['context_input_id']] for c in chunks])
    cosine=np.clip(qmat@nmat.T,-1,1);weights=np.array([q['weight'] for q in queries],dtype=np.float32);weights/=weights.max()
    weighted=cosine*weights[:,None];groups=defaultdict(list)
    for ci,ch in enumerate(chunks):
        qi=int(np.argmax(weighted[:,ci]));groups[ch['news_id']].append((float(weighted[qi,ci]),float(cosine[qi,ci]),qi,ci))
    output=[]
    for nid,hits in groups.items():
        hits.sort(key=lambda h:(-h[0],chunks[h[3]]['chunk_index']));score=hits[0][0]
        selected=[h for h in hits if h[0]>=settings.min_news_score][:settings.news_fragments]
        if not selected:selected=hits[:settings.news_fragments]
        matches=[];seen=set()
        for value,raw,qi,ci in sorted(((float(weighted[qi,ci]),float(cosine[qi,ci]),qi,ci) for qi in range(len(queries)) for _,_,_,ci in selected),key=lambda h:(-h[0],h[2],h[3])):
            if qi in seen:continue
            seen.add(qi);q=queries[qi]
            matches.append(dict(query_id=q['query_id'],channel=q['channel'],query=data['inputs'][q['input_id']]['text'],weight=q['weight'],normalized_weight=round(float(weights[qi]),6),cosine=round(raw,6),weighted_score=round(value,6),news_chunk_id=chunks[ci]['id'],evidence=json.loads(q['metadata_json'])))
            if len(matches)==3:break
        n=news[nid];meta=json.loads(n['source_json'])
        # Coverage is an explicit, separately labelled topical admission rule.
        # Its purpose is to recover macro events despite a low business cosine.
        # The ordinary similarity ranking/threshold remains unchanged.
        topic_matches=[]
        for topic,qids in requested.items():
            if not event_topic(n['title'],topic):continue
            best=max(((float(cosine[qi,h[3]]),qi,h[3]) for qi,q in enumerate(queries) if q['query_id'] in qids for h in selected),default=None)
            if best is None:continue
            raw,qi,ci=best;q=queries[qi];evidence=json.loads(q['metadata_json'])
            topic_matches.append(dict(topic=topic,query_id=q['query_id'],news_chunk_id=chunks[ci]['id'],cosine=round(raw,6),
                evidence_status=evidence.get('evidence_status'),exposure='conditional_not_confirmed',
                admission='explicit_profile_topic_and_headline_rule',below_similarity_threshold=score<settings.min_news_score,
                query_evidence=evidence))
        output.append(dict(news_id=nid,title=n['title'],url=n['url'],publisher=n['publisher'],publication_date=n['publication_date'],
          company_score=round(score,6),matching_queries=matches,document_limitations=meta.get('document_limitations',[]),
          preparation=meta.get('preparation',{}),source_blocks=meta.get('source_blocks',[]),
          selected_fragments=[{**json.loads(chunks[h[3]]['metadata_json']),
          'id':chunks[h[3]]['id'],'local_id':chunks[h[3]]['local_id'],'company_score':round(h[0],6)} for h in selected],
          selection_reason='company_similarity',financial_topic_matches=topic_matches,_chunks=[chunks[h[3]] for h in selected],_score=score))
    return output


def choose_company_news(data,company_id,settings,candidates,with_coverage=False):
    """Apply existing ranking/coverage limits to new or cached comparison results."""
    import copy
    output=copy.deepcopy(candidates)
    queries=[q for q in data['company_queries'] if q['company_id']==company_id and q['is_active'] and q['weight']>0]
    requested=financial_queries(queries,data['inputs']) if settings.financial_topic_coverage else {}
    coverage=dict(enabled=bool(settings.financial_topic_coverage),base_limit=settings.top_news,max_extra_news=min(len(requested),settings.financial_news_limit),financial_news_limit=settings.financial_news_limit,topics=[])
    def is_financial(n):return any(event_topic(n['title'],t) for t in TOPIC_PATTERNS)
    output.sort(key=lambda x:(-x['_score'],x['news_id']));chosen=[]
    for n in output:
        if n['_score']<settings.min_news_score:continue
        if is_financial(n) and sum(is_financial(x) for x in chosen)>=settings.financial_news_limit:continue
        chosen.append(n)
        if len(chosen)>=settings.top_news:break
    missing={t for t in requested if not any(any(m['topic']==t for m in n['financial_topic_matches']) for n in chosen)}
    extras=[n for n in output if n not in chosen and any(m['topic'] in missing for m in n['financial_topic_matches'])]
    extras.sort(key=lambda n:(-len({m['topic'] for m in n['financial_topic_matches']} & missing),-date.fromisoformat(n['publication_date']).toordinal(),-max(m['cosine'] for m in n['financial_topic_matches']),n['news_id']))
    for n in extras:
        if sum(is_financial(x) for x in chosen)>=settings.financial_news_limit:break
        if not any(m['topic'] in missing for m in n['financial_topic_matches']):continue
        n['selection_reason']='financial_topic_coverage';chosen.append(n)
        missing-={m['topic'] for m in n['financial_topic_matches']}
    for topic,qids in requested.items():
        matches=[n for n in chosen if any(m['topic']==topic for m in n['financial_topic_matches'])]
        status='added_for_topic_coverage' if any(n['selection_reason']=='financial_topic_coverage' for n in matches) else 'covered_in_base_selection'
        if not matches:
            candidates=[n for n in output if any(m['topic']==topic for m in n['financial_topic_matches'])]
            if candidates:
                status='omitted_financial_news_limit'
            else:status='no_qualified_fresh_event'
        coverage['topics'].append(dict(topic=topic,query_ids=qids,status=status,news_ids=[n['news_id'] for n in matches],
            limitation='Topic selection is not proof of company exposure; missing events are not generated.'))
    return (chosen,coverage) if with_coverage else chosen


def select_company_news(data,company_id,settings,with_coverage=False):
    return choose_company_news(data,company_id,settings,score_company_news(data,company_id,settings),with_coverage)

def cap_candidates(candidates,id_field,settings):
    candidates.sort(key=lambda x:(-x['_score'],x[id_field],x['news_chunk_id']))
    output=[];seen=set();docs=Counter()
    for item in candidates:
        # Keep the strongest query/fragment match for each result within the branch.
        key=item[id_field]
        if key in seen or docs[item['document_id']]>=settings.max_chunks_per_analytics:continue
        seen.add(key);docs[item['document_id']]+=1;item['rank']=len(output)+1;output.append(item)
        if len(output)>=settings.analytics_per_news:break
    return output

def agree(mechanism,source_candidates):
    refs=set(mechanism['source_fragment_ids'])
    hits=[s for s in source_candidates if s['news_chunk_id']==mechanism['news_chunk_id'] and s['source_chunk_id'] in refs]
    return bool(refs) and {s['source_chunk_id'] for s in hits}==refs,hits

def compare_analytics(data,news_day,news_chunks,settings):
    """Both branches use news_chunk.mechanism_input_id. Never use its company-context vector here."""
    documents={x['id']:x for x in data['analytics']};chunks={x['id']:x for x in data['analytics_chunks'] if x['is_active']}
    mechanisms=[x for x in data['analytics_mechanisms'] if x['is_active']]
    links=defaultdict(list)
    for link in data['analytics_mechanism_sources']:links[link['mechanism_id']].append(link['chunk_id'])
    allowed={};exclusions=Counter();published=date.fromisoformat(news_day)
    for did,doc in documents.items():
        when,precision=available(doc)
        if when is None:exclusions['unknown_analytics_date']+=1
        elif when>published:exclusions['future_analytics']+=1
        elif (published-when).days>settings.max_analytics_age_days:exclusions['old_analytics']+=1
        else:allowed[did]=precision
    arows=sorted([m for m in mechanisms if m['analytics_id'] in allowed],key=lambda m:m['id'])
    srows=sorted([s for s in chunks.values() if s['analytics_id'] in allowed],key=lambda s:s['id'])
    vec=data['vectors'];amat=np.stack([vec[m['input_id']] for m in arows]) if arows else np.empty((0,768),dtype=np.float32)
    smat=np.stack([vec[s['source_input_id']] for s in srows]) if srows else np.empty((0,768),dtype=np.float32)
    candidates=[];sources=[];missing=[]
    for news in news_chunks:
        if news['mechanism_input_id'] is None:missing.append(news['local_id']);continue
        q=vec[news['mechanism_input_id']]
        for row,value in zip(srows,np.clip(smat@q,-1,1)):
            if float(value)<settings.min_analytics_score:continue
            sources.append(dict(source_chunk_id=row['id'],document_id=row['analytics_id'],news_chunk_id=news['id'],
              source_provenance=source_provenance(row['analytics_id']),
              title=row['title'],similarity=round(float(value),6),_score=float(value),pages=json.loads(row['pages_json']),text=row['text'],metadata=json.loads(row['metadata_json'])))
        for row,value in zip(arows,np.clip(amat@q,-1,1)):
            if float(value)<settings.min_analytics_score:continue
            meta=json.loads(row['metadata_json']);refs=meta['source_fragment_ids']
            require(set(refs)==set(links[row['id']]) and set(refs)<=set(chunks),'Broken mechanism source links: '+row['id'])
            require(meta['source_text']=='\n\n'.join(chunks[cid]['text'] for cid in refs),'Archived source changed: '+row['id'])
            doc=documents[row['analytics_id']]
            candidates.append(dict(mechanism_id=row['id'],document_id=row['analytics_id'],news_chunk_id=news['id'],news_fragment_id=news['local_id'],
              title=row['title'],mechanism_similarity=round(float(value),6),_score=float(value),source_fragment_ids=refs,
              document_title=doc['title'],document_date=doc['document_date'],document_period=doc['document_period'],date_precision=allowed[row['analytics_id']],
              document_publisher=doc.get('publisher'),
              source_provenance=source_provenance(row['analytics_id']),
              source_path=doc['source_path'],source_sha256=doc['source_sha256'],mechanism=meta,
              source_fragments=[dict(id=cid,title=chunks[cid]['title'],text=chunks[cid]['text'],pages=json.loads(chunks[cid]['pages_json']),metadata=json.loads(chunks[cid]['metadata_json'])) for cid in refs],
              news_search_text=data['inputs'][news['mechanism_input_id']]['text'],relation_status='semantic_candidate_requires_context_review'))
    mtop=cap_candidates(candidates,'mechanism_id',settings);stop=cap_candidates(sources,'source_chunk_id',settings)
    accepted=[];rejected=[]
    for m in mtop:
        m['retrieval_agreement'],hits=agree(m,stop)
        m['source_similarities']=[dict(source_chunk_id=s['source_chunk_id'],similarity=s['similarity'],rank=s['rank']) for s in hits]
        (accepted if m['retrieval_agreement'] else rejected).append(m)
    for row in mtop+stop:row.pop('_score',None)
    return dict(analytics=accepted,mechanism_candidates=mtop,source_candidates=stop,not_in_intersection=rejected,
       diagnostics=dict(no_mechanism_fragments=missing,excluded_analytics_documents=dict(exclusions),allowed_documents=len(allowed)))

def retrieve(data,company_id,settings,selection=None):
    settings.validate();company=next(c for c in data['companies'] if c['id']==company_id)
    selected,coverage=selection if selection is not None else select_company_news(data,company_id,settings,with_coverage=True)
    for n in selected:
        n.update(compare_analytics(data,n['publication_date'],n.pop('_chunks'),settings));n.pop('_score')
    return dict(schema_version='comparison-report-1.0',company_id=company_id,subject_type=json.loads(company['source_json']).get('subject_type','company'),company_profile=company['profile_text'],
      company_facts={**json.loads(company['facts_json']),'counterparty_payments':counterparty_summary(json.loads(company['source_json']))},
      company_limitations=json.loads(company['source_json']).get('limitations',[]),financial_topic_coverage=coverage,
      as_of=settings.as_of,settings=asdict(settings),input_fingerprint=data['input_fingerprint'],
      algorithm=dict(company_news='max(weight/max_weight * cosine(company_query, news_context))',
        analytics='same news_mechanism query against analytics_mechanism AND analytics_source; independent top-k then intersection',
        agreement='same news fragment and every cited source fragment; report ID alone is insufficient',
        interpretation='Similarity is not a probability. Applicability and causal inference require subsequent review.'),news=selected,
      totals=dict(selected_news=len(selected),intersection_links=sum(len(n['analytics']) for n in selected),
                  mechanism_candidates=sum(len(n['mechanism_candidates']) for n in selected)))
