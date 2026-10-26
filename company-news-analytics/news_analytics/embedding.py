"""One local CPU model; role-specific prompts, immutable inputs and resumable batches."""
from __future__ import annotations
import hashlib,json,os
from importlib.metadata import version
from pathlib import Path
import numpy as np
from .common import dumps,sha,now,require,read_json
from .database import connect,backup

MODEL_ID='google/embeddinggemma-300m'
DIMENSIONS=768
LIMIT=2048

def identity(model_path):
    """Preserve the already validated cache fingerprint from the project prototype."""
    path=Path(model_path).resolve();require(path.is_dir(),f'Local model directory missing: {path}')
    config=read_json(path/'config_sentence_transformers.json');prompts=config['prompts']
    require(all(isinstance(prompts.get(k),str) and prompts[k] for k in ('query','document')),'Model query/document prompts missing')
    versions={name:version(name) for name in ('sentence-transformers','transformers','torch','numpy','tokenizers')}
    inference={'pipeline':'analytics-embedding-v1','document_prompt':prompts['document'],
       'method':'encode_document','dtype':'float32','device':'cpu','normalized':True,'dimensions':DIMENSIONS,'versions':versions}
    digest=hashlib.sha256(json.dumps(inference,sort_keys=True).encode('utf-8'))
    files=sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in {'.json','.safetensors','.model','.bin'}
                 and not any(part.startswith('.') for part in p.relative_to(path).parts))
    require(any(p.suffix=='.safetensors' for p in files),'Local safetensors weights missing')
    for f in files:
        digest.update(f.relative_to(path).as_posix().encode('utf-8')+b'\0');filehash=hashlib.sha256()
        with f.open('rb') as stream:
            while block:=stream.read(8*1024*1024):filehash.update(block)
        digest.update(filehash.digest())
    return dict(model_id=MODEL_ID,model_revision='local-sha256:'+digest.hexdigest(),dimensions=DIMENSIONS,prompts=prompts,versions=versions)

def vector(row,model):
    r=dict(row)
    if not r.get('embedding'):return None,'missing_vector'
    if r.get('text_hash')!=sha(r['text']) or r.get('embedded_input_hash')!=r['text_hash']:return None,'stale_text'
    if (r.get('model_id'),r.get('model_revision'),r.get('dimensions'))!=(model['model_id'],model['model_revision'],DIMENSIONS):return None,'different_model'
    method=r['encoding_method'];expected='encode_query' if r['role'] in {'company_query','news_mechanism'} else 'encode_document'
    if method!=expected or r.get('prompt')!=model['prompts']['query' if method=='encode_query' else 'document']:return None,'wrong_encoding_role'
    if r.get('normalized')!=1 or not r.get('embedded_at') or type(r.get('token_count')) is not int or not 0<r['token_count']<=LIMIT:return None,'invalid_provenance'
    blob=r['embedding']
    if not isinstance(blob,bytes) or len(blob)!=3072:return None,'invalid_vector_bytes'
    v=np.frombuffer(blob,dtype='<f4');norm=float(np.linalg.norm(v))
    if not np.isfinite(v).all() or abs(norm-1)>1e-4:return None,'invalid_vector_norm'
    return v/norm,None

def embed(db,model_path,model=None,batch_size=4,threads=4,runtime=None,company_id=None):
    require(type(batch_size) is int and batch_size>0 and type(threads) is int and threads>0,'batch_size and threads must be positive integers')
    model=model or identity(model_path)
    where=' WHERE id IN (SELECT input_id FROM company_queries WHERE company_id=? AND is_active=1)' if company_id else ''
    with connect(db,True) as c:rows=[dict(r) for r in c.execute('SELECT * FROM active_embedding_inputs'+where+' ORDER BY encoding_method,id', (company_id,) if company_id else ())]
    pending=[r for r in rows if vector(r,model)[1]]
    stats=dict(total=len(rows),reused=len(rows)-len(pending),computed=0,roles={})
    for role in sorted({r['role'] for r in rows}):stats['roles'][role]=dict(total=sum(r['role']==role for r in rows),pending=sum(r['role']==role for r in pending))
    if not pending:return stats
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1';os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    import torch
    from transformers import AutoTokenizer
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(threads)
    runtime=runtime if runtime is not None else {}
    signature=(str(Path(model_path).resolve()),model['model_revision'],threads)
    if runtime.get('signature')!=signature:
        runtime.clear()
        runtime['signature']=signature
    tokenizer=runtime.get('tokenizer')
    if tokenizer is None:
        tokenizer=AutoTokenizer.from_pretrained(str(model_path),local_files_only=True)
        runtime['tokenizer']=tokenizer
    for row in pending:
        prompt=model['prompts']['query' if row['encoding_method']=='encode_query' else 'document']
        row['actual_tokens']=len(tokenizer.encode(prompt+row['text'],add_special_tokens=True,truncation=False))
        require(0<row['actual_tokens']<=LIMIT,f"Input {row['id']} ({row['role']}) has {row['actual_tokens']} tokens; split the fragment, truncation refused")
    encoder=runtime.get('encoder')
    if encoder is None:
        encoder=SentenceTransformer(str(model_path),device='cpu',local_files_only=True,model_kwargs={'dtype':torch.float32})
        runtime['encoder']=encoder
    require(encoder.get_embedding_dimension()==DIMENSIONS,'Model dimension differs from 768')
    require(all(encoder.prompts.get(k)==v for k,v in model['prompts'].items() if k in {'document','query'}),'Model prompts changed')
    require(max(r['actual_tokens'] for r in pending)<=encoder.max_seq_length,'Model input limit too small; truncation refused')
    stats['backup']=backup(db)
    for method in ('encode_document','encode_query'):
        selected=[r for r in pending if r['encoding_method']==method]
        for start in range(0,len(selected),batch_size):
            items=selected[start:start+batch_size]
            values=np.asarray(getattr(encoder,method)([r['text'] for r in items],batch_size=batch_size,normalize_embeddings=True,
                               convert_to_numpy=True,show_progress_bar=False),dtype='<f4')
            require(values.shape==(len(items),DIMENSIONS) and np.isfinite(values).all(),'Model returned invalid vectors')
            require(np.allclose(np.linalg.norm(values,axis=1),1,atol=1e-5),'Model returned unnormalized vectors')
            with connect(db) as c:
                with c:
                    for row,v in zip(items,values):
                        changed=c.execute('''UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,
                         normalized=1,embedded_input_hash=text_hash,prompt=?,token_count=?,embedded_at=?
                         WHERE id=? AND text_hash=? AND encoding_method=? AND id IN (SELECT id FROM active_embedding_inputs)''',
                         (v.tobytes(),model['model_id'],model['model_revision'],model['prompts']['query' if method=='encode_query' else 'document'],
                          row['actual_tokens'],now(),row['id'],row['text_hash'],method))
                        require(changed.rowcount==1,'Input became inactive during inference; batch rolled back')
            stats['computed']+=len(items);print(f"Vectors: {stats['computed']}/{len(pending)}",flush=True)
    return stats

def reuse_legacy(db,legacy_path,model):
    """Explicit one-time cache transfer; nothing is changed in the old database."""
    sources=[('company_queries','query_text','company_query'),('article_chunks','embedding_text','news_context'),
             ('article_analytics_queries','embedding_text','news_mechanism'),('analytics_chunks','embedding_text','analytics_source'),
             ('analytics_mechanisms','embedding_text','analytics_mechanism')]
    candidates={};rejected=0
    with connect(legacy_path,True) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'embedding_inputs' in tables:
            for row in c.execute('SELECT * FROM embedding_inputs WHERE embedding IS NOT NULL'):
                converted=dict(row)
                if vector(converted,model)[1]:rejected+=1;continue
                candidates[(converted['role'],converted['text_hash'])]=converted
        for table,column,role in sources:
            if table not in tables:continue
            if 'embedding' not in {r[1] for r in c.execute('PRAGMA table_info('+table+')')}:continue
            for row in c.execute(f'SELECT * FROM {table} WHERE embedding IS NOT NULL'):
                r=dict(row);method='encode_query' if role in {'company_query','news_mechanism'} else 'encode_document'
                prompt=model['prompts']['query' if method=='encode_query' else 'document']
                # Legacy document fingerprint includes the document prompt; old document rows did not store the role separately.
                converted=dict(role=role,text=r[column],text_hash=r.get('content_hash'),embedding=r['embedding'],
                  model_id=r.get('embedding_model'),model_revision=r.get('embedding_revision'),dimensions=r.get('embedding_dimensions'),
                  normalized=r.get('embedding_normalized'),embedded_input_hash=r.get('embedded_input_hash'),
                  encoding_method=r.get('encoding_method') or method,prompt=r.get('encoding_prompt') or prompt,
                  token_count=r.get('token_count'),embedded_at=r.get('embedded_at'))
                if method=='encode_query' and r.get('encoding_method')!='encode_query':rejected+=1;continue
                if vector(converted,model)[1]:rejected+=1;continue
                candidates[(role,sha(r[column]))]=converted
    copied=0
    with connect(db) as c:
        with c:
            for row in c.execute('SELECT * FROM active_embedding_inputs').fetchall():
                if not vector(row,model)[1]:continue
                cached=candidates.get((row['role'],row['text_hash']))
                if not cached or cached['text']!=row['text']:continue
                c.execute('''UPDATE embedding_inputs SET embedding=?,model_id=?,model_revision=?,dimensions=768,normalized=1,
                  embedded_input_hash=text_hash,prompt=?,token_count=?,embedded_at=? WHERE id=?''',
                  (cached['embedding'],model['model_id'],model['model_revision'],cached['prompt'],cached['token_count'],cached['embedded_at'],row['id']))
                copied+=1
    return dict(copied=copied,rejected_legacy_vectors=rejected)
