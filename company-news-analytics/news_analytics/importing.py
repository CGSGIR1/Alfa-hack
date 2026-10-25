from __future__ import annotations
from collections import Counter
from pathlib import Path
import copy,sqlite3
from .common import dumps,sha,now,require
from .database import connect,backup,check
from .validation import load_inputs,news_url_key

def input_id(c,role,body):
    method='encode_query' if role in {'company_query','news_mechanism'} else 'encode_document'
    digest=sha(body)
    c.execute('INSERT OR IGNORE INTO embedding_inputs(role,text,text_hash,encoding_method) VALUES(?,?,?,?)',(role,body,digest,method))
    row=c.execute('SELECT id,text FROM embedding_inputs WHERE role=? AND text_hash=?',(role,digest)).fetchone()
    require(row['text']==body,'Embedding input hash collision');return row['id']

def upsert(c,table,values,keys=('id',)):
    columns=list(values);assignments=','.join(f'{k}=excluded.{k}' for k in columns if k not in keys)
    c.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) '
              f'ON CONFLICT({",".join(keys)}) DO UPDATE SET {assignments}',tuple(values.values()))

def owner(c,table,identifier,column,document_id):
    found=c.execute(f'SELECT {column} FROM {table} WHERE id=?',(identifier,)).fetchone()
    require(not found or found[0]==document_id,f'{table}: ID belongs to another document: {identifier}')

def company(c,path,d):
    uid=d['subject_id'];stamp=now();old=c.execute('SELECT created_at FROM companies WHERE id=?',(uid,)).fetchone()
    upsert(c,'companies',dict(id=uid,profile_text=d['profile_text'],facts_json=dumps(d['facts']),source_json=dumps(d),
        input_hash=sha(dumps(d)),input_file=str(path),created_at=old[0] if old else stamp,updated_at=stamp))
    c.execute('UPDATE company_queries SET is_active=0 WHERE company_id=?',(uid,))
    for index,q in enumerate(d['queries'],1):
        upsert(c,'company_queries',dict(company_id=uid,query_id=q['id'],query_index=index,channel=q['channel'],
          weight=q['weight'],input_id=input_id(c,'company_query',q['text']),metadata_json=dumps(q),is_active=1),('company_id','query_id'))

def news(c,path,d):
    uid=d['document_id'];stamp=now();old=c.execute('SELECT created_at,url FROM news WHERE id=?',(uid,)).fetchone()
    require(not old or news_url_key(old['url'])==news_url_key(d['source']['url']),'Changing a news URL requires a new document ID')
    upsert(c,'news',dict(id=uid,title=d['document_title'],url=d['source']['url'],publisher=d['source'].get('publisher'),
      publication_date=d['document_date'],source_json=dumps(d),input_hash=sha(dumps(d)),input_file=str(path),
      created_at=old['created_at'] if old else stamp,updated_at=stamp))
    c.execute('UPDATE article_chunks SET is_active=0 WHERE news_id=?',(uid,))
    for index,f in enumerate(d['fragments'],1):
        cid=uid+'::'+f['id'];owner(c,'article_chunks',cid,'news_id',uid)
        upsert(c,'article_chunks',dict(id=cid,news_id=uid,local_id=f['id'],chunk_index=index,thesis=f['thesis'],context=f['context'],
          text=f['thesis']+'\n'+f['context'],context_input_id=input_id(c,'news_context',f['company_embedding_text']),
          mechanism_input_id=input_id(c,'news_mechanism',f['analytics_embedding_text']) if f['analytics_embedding_text'] else None,
          metadata_json=dumps(f),is_active=1))

def analytics(c,path,d):
    doc=d['document'];uid=doc['id'];stamp=now()
    old=c.execute('SELECT source_sha256,created_at FROM analytics WHERE id=?',(uid,)).fetchone()
    require(not old or old['source_sha256']==doc['source_sha256'],'Changed source PDF requires a new analytics document ID')
    existing={r['id']:r['text'] for r in c.execute('SELECT id,text FROM analytics_chunks WHERE analytics_id=?',(uid,))}
    require(all(ch['id'] not in existing or existing[ch['id']]==ch['text'] for ch in d['chunks']),
            'Changing archived analytical source text requires new source chunk IDs')
    upsert(c,'analytics',dict(id=uid,title=doc['title'],publisher=doc.get('publisher'),document_date=doc.get('date'),
      document_period=doc.get('period'),source_sha256=doc['source_sha256'],source_path=doc.get('source_path'),
      source_json=dumps(d),input_hash=sha(dumps(d)),input_file=str(path),created_at=old['created_at'] if old else stamp,updated_at=stamp))
    for page in d['source_pages']:
        upsert(c,'analytics_pages',dict(analytics_id=uid,page_number=page['page'],text=page['text'],metadata_json=dumps(page)),('analytics_id','page_number'))
    for key,kind in [('charts','chart'),('tables','table')]:
        for v in d.get(key,[]):upsert(c,'analytics_visuals',dict(analytics_id=uid,local_id=v['id'],page_number=v['page'],kind=kind,title=v['title'],metadata_json=dumps(v)),('analytics_id','local_id'))
    c.execute('UPDATE analytics_chunks SET is_active=0 WHERE analytics_id=?',(uid,))
    c.execute('UPDATE analytics_mechanisms SET is_active=0 WHERE analytics_id=?',(uid,))
    for index,ch in enumerate(d['chunks'],1):
        owner(c,'analytics_chunks',ch['id'],'analytics_id',uid)
        upsert(c,'analytics_chunks',dict(id=ch['id'],analytics_id=uid,local_id=ch['local_id'],chunk_index=index,title=ch['title'],text=ch['text'],
          pages_json=dumps(ch.get('pages',[ch.get('page')])),source_input_id=input_id(c,'analytics_source',ch['embedding_text']),metadata_json=dumps(ch),is_active=1))
    for m in d['mechanisms']:
        owner(c,'analytics_mechanisms',m['id'],'analytics_id',uid)
        upsert(c,'analytics_mechanisms',dict(id=m['id'],analytics_id=uid,title=m['title'],input_id=input_id(c,'analytics_mechanism',m['embedding_text']),
          metadata_json=dumps(m),is_active=int(m['eligible_for_retrieval'])))
        c.execute('DELETE FROM analytics_mechanism_sources WHERE mechanism_id=?',(m['id'],))
        for ref in m['source_fragment_ids']:
            c.execute('INSERT INTO analytics_mechanism_sources VALUES(?,?,?,?)',(m['id'],uid,ref,dumps([e for e in m['evidence_refs'] if e['fragment_id']==ref])))

IMPORTERS={'companies':company,'news':news,'analytics':analytics}
def import_all(db,locations,update_existing=False,skip_invalid_news=False):
    # Validate every document of every selected kind before starting any data writes.
    issues=[];aliases=[]
    batches={kind:load_inputs(path,kind,skip_invalid=skip_invalid_news and kind=='news',issues=issues) for kind,path in locations.items() if path is not None}
    summary={kind:dict(validated=len(batch),added=0,updated=0,skipped=0,changed_skipped=0) for kind,batch in batches.items()}
    changes=[]
    with connect(db,True) as c:
        known_urls={news_url_key(r['url']):r['id'] for r in c.execute('SELECT id,url FROM news')}
        for kind,batch in batches.items():
            table='companies' if kind=='companies' else kind
            for path,d in batch:
                if kind=='news':
                    key=news_url_key(d['source']['url']);existing_id=known_urls.get(key)
                    if existing_id and existing_id!=d['document_id']:
                        # A different preparation ID is a new representation of the same article.
                        # Preserve the stable DB identity and retain the incoming ID as provenance.
                        incoming=d['document_id'];collision=c.execute('SELECT url FROM news WHERE id=?',(incoming,)).fetchone()
                        if collision and news_url_key(collision[0])!=key:
                            if not skip_invalid_news:raise ValueError('News ID belongs to another URL: '+incoming)
                            issues.append(dict(file=str(path),reason='News ID belongs to another URL: '+incoming,stage='identity'));continue
                        d=copy.deepcopy(d);d['document_id']=existing_id;d.setdefault('import_provenance',{})['incoming_document_id']=incoming
                        aliases.append(dict(file=str(path),incoming_id=incoming,database_id=existing_id,url=d['source']['url']))
                    known_urls[key]=d['document_id']
                uid=d['subject_id'] if kind=='companies' else d['document_id'] if kind=='news' else d['document']['id']
                row=c.execute(f'SELECT input_hash FROM {table} WHERE id=?',(uid,)).fetchone()
                changed=bool(row and row[0]!=sha(dumps(d)))
                if row and (not changed or not update_existing):
                    summary[kind]['skipped']+=1;summary[kind]['changed_skipped']+=int(changed)
                else:changes.append((kind,path,d,bool(row)))
    saved=backup(db) if changes else None
    with connect(db) as c:
        with c:
            c.execute('BEGIN IMMEDIATE')
            for kind,path,d,existed in changes:
                c.execute('SAVEPOINT document_import')
                try:IMPORTERS[kind](c,path.resolve(),d)
                except (ValueError,KeyError,TypeError,IndexError,sqlite3.IntegrityError) as exc:
                    c.execute('ROLLBACK TO document_import');c.execute('RELEASE document_import')
                    if not (skip_invalid_news and kind=='news'):raise
                    issues.append(dict(file=str(path),reason=str(exc),stage='database'));continue
                c.execute('RELEASE document_import');summary[kind]['updated' if existed else 'added']+=1
            check(c)
    return dict(kinds=summary,backup=saved,changed_documents=sum(s['added']+s['updated'] for s in summary.values()),rejected_news=issues,rejected_count=len(issues),news_identity_aliases=aliases)
