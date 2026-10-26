"""Validate prepared JSON. No LLM calls and no silent repair of source facts."""
from __future__ import annotations
import copy,re
from urllib.parse import urlsplit, urlunsplit
from datetime import date
from pathlib import Path
from .common import dumps,sha,text,require,number,read_json
from .analytics_sources import source_provenance

NEWS_VERSION='news-two-views-1.0'

def news_url_key(value):
    url=text(value,'source.url').strip()
    parts=urlsplit(url)
    require(parts.scheme.lower() in ('http','https') and bool(parts.hostname) and not parts.username and not parts.password,'News URL must be an absolute HTTP(S) URL')
    host=parts.hostname.lower().removeprefix('www.')
    port=parts.port
    if port and (parts.scheme.lower(),port) not in {('https',443),('http',80)}:host+=':'+str(port)
    return urlunsplit((parts.scheme.lower(),host,parts.path or '/',parts.query,''))

def substantive_quote(value):
    """A mechanical minimum, not a semantic entailment/accuracy check."""
    return isinstance(value,str) and len(value.strip())>=40 and len(re.findall(r'\w+',value))>=6


def news_evidence(d,required=False):
    blocks=d.get('source_blocks')
    require(not required or isinstance(blocks,list) and blocks,'News preparation requires original source_blocks')
    if blocks is None:return  # Existing legacy fixtures remain importable, visibly flagged in the packet.
    require(isinstance(blocks,list) and blocks,'source_blocks must be a nonempty array')
    byid={}
    for b in blocks:
        require(isinstance(b,dict),'Source block must be an object')
        bid=text(b.get('id'),'source block ID');require(bid not in byid,'Duplicate source block ID')
        byid[bid]=text(b.get('text'),'source block text')
    for f in d['fragments']:
        refs=f.get('source_block_ids',[])
        require(isinstance(refs,list) and refs and all(isinstance(b,str) and b in byid for b in refs),'News fragment needs known source block IDs')
        quotes=f.get('source_quotes',[])
        require(isinstance(quotes,list) and quotes and all(isinstance(q,str) and q and any(q in byid[b] for b in refs) for q in quotes),'News quote absent from cited source blocks')
        require(all(substantive_quote(q) for q in quotes),'News quotes too short: cite a substantive exact passage (at least 40 characters and 6 words)')
        for a in f.get('attributions',[])+f.get('mechanism',{}).get('source_attributions',[]):
            require(isinstance(a,dict) and a.get('source_block_id') in refs and isinstance(a.get('quote'),str) and a['quote'] and a['quote'] in byid[a['source_block_id']],
                    'Attribution needs an exact quote in a cited source block')
def day(value,label,optional=False):
    if value is None and optional:return None
    require(isinstance(value,str),f'{label}: ISO date required')
    require(date.fromisoformat(value).isoformat()==value,f'{label}: YYYY-MM-DD required');return value
def strings(values,label):
    require(isinstance(values,list) and all(isinstance(x,str) for x in values),f'{label}: string array required')
    return values
def pointer(data,path):
    require(isinstance(path,str) and path.startswith('/'),'Invalid source_refs JSON pointer')
    for key in path[1:].split('/'):
        key=key.replace('~1','/').replace('~0','~')
        data=data[int(key)] if isinstance(data,list) else data[key]
    return data

def company(data):
    d=copy.deepcopy(data);require(isinstance(d,dict),'Company root must be an object')
    require(d.get('schema_version') in {'company-profile-2.0','company-profile-1.0'},'Unsupported company schema; a prepared profile with facts and queries is required')
    text(d.get('subject_id'),'subject_id');text(d.get('profile_text'),'profile_text')
    require(isinstance(d.get('facts'),dict),'facts object required; CSV is not read implicitly')
    require(d.get('subject_type','company') in {'company','group'},'Expected company or group subject_type')
    if d.get('subject_type')=='group':
        require(isinstance(d['facts'].get('members'),list) and d['facts']['members'],'Group must identify its known members')
    raw=d.get('provenance',{}).get('company_source',{}).get('raw_fields',{})
    require(not raw or raw.get('SYNTHETIC_U')==d['subject_id'],'Company provenance ID mismatch')
    queries=d.get('queries');require(isinstance(queries,list) and queries,'queries must be nonempty')
    ids=set()
    for q in queries:
        require(isinstance(q,dict),'Query must be an object')
        channel=text(q.get('channel'),'query.channel');qid=text(q.get('id') or 'channel:'+channel,'query.id')
        require(qid not in ids,'Duplicate company query ID: '+qid);ids.add(qid);q['id']=qid
        text(q.get('text'),'query.text');number(q.get('weight',1),'query.weight');q.setdefault('weight',1)
        for ref in q.get('source_refs',[]):pointer(d,ref)
    require(any(q['weight']>0 for q in queries),'At least one positive-weight company query required')
    return d

def news(data):
    d=copy.deepcopy(data);require(isinstance(d,dict),'News root must be an object')
    # Event-identity experiment is disabled; old enriched copies must not reactivate it.
    d.pop('event_identity',None)
    require(d.get('schema_version') in {'news-dual-embeddings-2.0',NEWS_VERSION},'Unsupported news schema')
    text(d.get('document_id'),'document_id');text(d.get('document_title'),'document_title')
    day(d.get('document_date'),'document_date')
    source=d.get('source');require(isinstance(source,dict),'source required')
    source['url']=text(source.get('url'),'source.url').strip();news_url_key(source['url'])
    fragments=d.get('fragments');require(isinstance(fragments,list) and fragments,'News fragments must be nonempty')
    ids=set()
    for f in fragments:
        require(isinstance(f,dict),'News fragment must be an object')
        fid=text(f.get('id'),'fragment.id');require(fid not in ids,'Duplicate news fragment ID');ids.add(fid)
        text(f.get('title'),'fragment.title');text(f.get('thesis'),'thesis');text(f.get('context'),'context')
        body=text(f.get('company_embedding_text'),'company_embedding_text')
        require(f['thesis'] in body and f['context'] in body,'Company-facing news text must include exact thesis and context')
        m=f.get('mechanism');require(isinstance(m,dict),'mechanism object required')
        require(m.get('status') in {'not_identified','hypothesis','attributed_in_news'},'Invalid mechanism status')
        require(m.get('is_established_cause') is False,'Search hypothesis must not establish causality')
        strings(m.get('boundaries'),'mechanism.boundaries')
        mechanism_context=m.get('context')
        require(isinstance(mechanism_context,dict),'mechanism.context must be an object')
        conditions=text(mechanism_context.get('conditions'),'mechanism.context.conditions')
        if m['status']=='attributed_in_news':
            # Earlier prepared JSON stored attribution on the fragment, not inside mechanism.
            if not m.get('source_attributions') and f.get('attributions'):
                m['source_attributions']=copy.deepcopy(f['attributions']);m['source_attributions_origin']='fragment_attributions_compatibility'
            require(m.get('source_attributions'),'Attributed explanation needs source_attributions')
        if m['status']=='not_identified':
            require(f.get('analytics_embedding_text') is None,'No mechanism: analytics_embedding_text must be null')
            f['analytics_search_question']=None;f['analytics_embedding_text']=None
        else:
            text(m.get('description'),'mechanism.description')
            q=f.get('analytics_search_question')
            if not q:q=text(f.get('analytics_embedding_text'),'analytics_embedding_text').split('\nУсловия и границы применимости:',1)[0]
            text(q,'analytics_search_question');f['analytics_search_question']=q
            f['analytics_embedding_text']=q+'\nУсловия и границы применимости: '+conditions
        # The rejected third news role is neither persisted nor encoded.
        f.pop('analytics_source_embedding_text',None)
        f['analytics_query_version']=NEWS_VERSION
        f['embedding_roles']={'company':'encode_document','analytics':'encode_query'}
        strings(f.get('limitations',[]),'fragment.limitations')
    news_evidence(d)
    d['schema_version']=NEWS_VERSION;d.setdefault('preparation',{})['representation_version']=NEWS_VERSION
    return d

def analytics(data):
    d=copy.deepcopy(data);require(isinstance(d,dict),'Analytics root must be an object')
    require(d.get('schema_version')=='analytics-contextual-mechanisms-2.0','Unsupported analytics schema')
    doc=d['document'];did=text(doc.get('id'),'document.id');text(doc.get('title'),'document.title')
    provenance=source_provenance(did)
    if provenance['provided_by_organizer']:doc['source_provenance']=provenance
    require(bool(re.fullmatch('[0-9a-f]{64}',str(doc.get('source_sha256','')))),'Invalid source PDF SHA-256')
    day(doc.get('date'),'document.date',True)
    if doc.get('period'):
        require(bool(re.fullmatch(r'\d{4}-\d{2}',doc['period'])),'Invalid document.period');date.fromisoformat(doc['period']+'-01')
    pages=d.get('source_pages');require(isinstance(pages,list) and pages,'source_pages required')
    pageids=set()
    for p in pages:
        n=p.get('page');require(type(n) is int and n>=1 and n not in pageids,'Invalid/duplicate page number');pageids.add(n)
        require(isinstance(p.get('text'),str),'Page text required')
    require(type(doc.get('page_count')) is int and doc['page_count']==len(pageids),'Page count mismatch')
    visuals=set()
    for key in ('charts','tables'):
        for v in d.get(key,[]):
            vid=text(v.get('id'),'visual.id');require(vid not in visuals,'Duplicate visual ID');visuals.add(vid)
            require(v.get('page') in pageids,'Visual page missing');text(v.get('title'),'visual.title')
    chunks=d.get('chunks');require(isinstance(chunks,list) and chunks,'Analytics chunks required')
    byid={};locals_=set()
    for ch in chunks:
        cid=text(ch.get('id'),'chunk.id');local=text(ch.get('local_id'),'chunk.local_id')
        require(cid not in byid and local not in locals_,'Duplicate analytics chunk ID');byid[cid]=ch;locals_.add(local)
        require(ch.get('document_id')==did,'Chunk belongs to another document')
        for k in ('title','text','embedding_text'):text(ch.get(k),'chunk.'+k)
        require(ch['text'] in ch['embedding_text'],'Source embedding text must contain the complete original fragment')
        require(ch.get('content_sha256')==sha(ch['embedding_text']),'Stale source embedding text hash')
        ps=ch.get('pages',[ch.get('page')]);require(isinstance(ps,list) and ps and all(type(n) is int and n in pageids for n in ps),'Invalid chunk pages')
        require(set(ch.get('related_visual_ids',[]))<=visuals,'Unknown chunk visual')
    mechanisms=d.get('mechanisms');require(isinstance(mechanisms,list),'mechanisms array required, possibly empty')
    mids=set();coverage={cid:[] for cid in byid}
    for m in mechanisms:
        mid=text(m.get('id'),'mechanism.id');require(mid not in mids,'Duplicate mechanism ID');mids.add(mid)
        require(m.get('document_id')==did,'Mechanism belongs to another document')
        require(type(m.get('eligible_for_retrieval')) is bool,'Mechanism eligibility must be boolean')
        require(m.get('encoding_method')=='encode_document','Analytics mechanism uses document encoding')
        require(m.get('statement_type') in {'author_explanation','author_hypothesis','author_forecast','attributed_explanation','correlation'},'Invalid analytical statement type')
        body=text(m.get('embedding_text'),'mechanism.embedding_text')
        require(m.get('content_sha256')==sha(body),'Stale mechanism text hash')
        for name in ('cause','channel','effect'):
            value=text(m.get('mechanism',{}).get(name),'mechanism.'+name);require(value in body,'Mechanism text omits '+name)
        conditions=text(m.get('context',{}).get('conditions'),'mechanism.context.conditions')
        require(conditions in body,'Mechanism text omits applicability conditions')
        refs=m.get('source_fragment_ids');require(isinstance(refs,list) and refs and len(set(refs))==len(refs) and set(refs)<=set(byid),'Invalid mechanism source IDs')
        require(m.get('source_text')=='\n\n'.join(byid[cid]['text'] for cid in refs),'Mechanism source_text differs from original')
        expectedpages={n for cid in refs for n in byid[cid].get('pages',[byid[cid].get('page')])}
        require(set(m.get('source_pages',[]))==expectedpages,'Mechanism page mismatch')
        evidence=m.get('evidence_refs');require(isinstance(evidence,list) and evidence,'Evidence required')
        evidenced=set()
        for e in evidence:
            cid=e.get('fragment_id');require(cid in refs,'Evidence source mismatch')
            require(text(e.get('quote'),'evidence.quote') in byid[cid]['text'],'Evidence quote absent from source')
            require(set(e.get('pages',[]))<=set(byid[cid].get('pages',[byid[cid].get('page')])),'Evidence page mismatch');evidenced.add(cid)
        require(evidenced==set(refs),'Every cited source needs evidence')
        require(set(m.get('related_visual_ids',[]))<=visuals,'Unknown mechanism visual')
        strings(m.get('limitations',[]),'mechanism.limitations')
        for cid in refs:coverage[cid].append(mid)
    for cid,ch in byid.items():require(set(ch.get('mechanism_ids',[]))==set(coverage[cid]),'Mechanism coverage mismatch for '+cid)
    return d

VALIDATORS={'companies':company,'news':news,'analytics':analytics}
def load_inputs(location,kind,skip_invalid=False,issues=None):
    path=Path(location);require(path.exists(),f'Input not found: {path}')
    files=[path] if path.is_file() else sorted(path.rglob('*.json'));require(files,f'No JSON files in {path}')
    output=[];ids=set();urls=set()
    for file in files:
        try:
            data=VALIDATORS[kind](read_json(file))
            did=data['subject_id'] if kind=='companies' else data['document_id'] if kind=='news' else data['document']['id']
            require(did not in ids,'Duplicate document ID in input batch: '+did)
            if kind=='news':
                key=news_url_key(data['source']['url'])
                require(key not in urls,'Duplicate news URL in batch: '+data['source']['url']);urls.add(key)
            ids.add(did)
            output.append((file,data))
        except (ValueError,KeyError,TypeError,IndexError,UnicodeError) as exc:
            if not skip_invalid:raise ValueError(f'{file}: {exc}') from exc
            if issues is not None:issues.append(dict(file=str(file),reason=str(exc),stage='validation'))
    return output
