"""Verify the immutable source delivery; runtime/model directories are not hashed."""
from pathlib import Path
import hashlib,json,re

ROOT=Path(__file__).resolve().parents[1]
MODEL_SUFFIXES={'.safetensors','.bin','.model','.onnx','.gguf','.pt','.pth'}
SECRET=re.compile(r'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{24,}|sk-(?:proj-)?[A-Za-z0-9_-]{35,})\b')
LOCAL_PATH=re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]')


def main():
    manifest=json.loads((ROOT/'release-manifest.json').read_text(encoding='utf-8'))
    errors=[]
    required={'config.json','run.py','start_ui.py','Dockerfile','compose.yaml','requirements.txt','requirements.lock','news_analytics/workflow.py','news_analytics/schema.sql'}
    if not required<={r['path'] for r in manifest['files']}:errors.append('Required application files missing from manifest')
    for row in manifest['files']:
        rel=Path(row['path']);path=(ROOT/rel).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():errors.append('Missing/unsafe: '+str(rel));continue
        if path.suffix.lower() in MODEL_SUFFIXES or path.name=='.env' or path.suffix in {'.db','.sqlite','.sqlite3'}:errors.append('Forbidden source file: '+str(rel))
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=row['sha256'] or path.stat().st_size!=row['bytes']:errors.append('Changed: '+str(rel))
        if path.suffix.lower() in {'.py','.json','.md','.toml','.yaml','.yml','.txt','.cmd','.sql','.example'}:
            body=path.read_text(encoding='utf-8-sig')
            if SECRET.search(body):errors.append('Possible credential: '+str(rel))
            if LOCAL_PATH.search(body):errors.append('Absolute local path: '+str(rel))
    if errors:raise SystemExit('\n'.join(errors))
    print(json.dumps(dict(passed=True,files=len(manifest['files']),model_weights_included=False),ensure_ascii=False))


if __name__=='__main__':main()
