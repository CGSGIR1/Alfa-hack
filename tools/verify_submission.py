"""Verify the source manifest and reject runtime state, weights and credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

SECRET = re.compile(rb'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{24,}|sk-(?:proj-)?[A-Za-z0-9_-]{35,})\b')
WEIGHTS = {'.safetensors','.gguf','.onnx','.pt','.pth','.bin','.model'}
FORBIDDEN = {'.venv','venv','__pycache__','runtime','models','.cache','exports','precomputed','experiments','reports'}


def verify(root, key=None, git_index=True):
    root = Path(root).resolve()
    manifest = json.loads((root/'submission-manifest.json').read_text(encoding='utf-8'))
    expected = {row['path']:row for row in manifest['files']}
    required = {'app.py','config.json','README.md','Dockerfile','Dockerfile.with-model',
                'compose.bundled.yaml','.env.example','src/integration/web.py','docs/API_KEYS.md'}
    errors = []
    if not required <= expected.keys():errors.append('Missing required application files')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts}
    if actual != set(expected)|{'submission-manifest.json'}:errors.append('Source tree differs from manifest')
    for name,row in expected.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append('Missing/unsafe path: '+name);continue
        rel=Path(name)
        if (any(part in FORBIDDEN for part in rel.parts) or rel.parts[0]=='data'
            or (rel.name.startswith('.env') and rel.name!='.env.example')
            or rel.suffix.lower() in WEIGHTS|{'.db','.sqlite','.sqlite3','.log','.tar','.zip'}):
            errors.append('Forbidden file: '+name)
        body=path.read_bytes()
        if len(body)!=row['bytes'] or hashlib.sha256(body).hexdigest()!=row['sha256']:
            errors.append('Hash differs: '+name)
        private_marker=b'-----BEGIN '+b'PRIVATE KEY-----'
        if SECRET.search(body) or (key and key in body) or private_marker in body:
            errors.append('Possible credential in: '+name)
    for folder, count, suffix in (('csv',3,'.csv'),('companies',10,'.json'),('analytics',22,'.json'),('news',614,'.json')):
        if len(list((root/'private-data'/folder).glob('*'+suffix))) != count:
            errors.append('Private data missing or incomplete: '+folder)
    if list(root.rglob('*.pdf')):errors.append('PDF files should not be in this release')
    indexed=None
    if git_index and (root/'.git').exists():
        tracked=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\x00')
        indexed=set(tracked)-{''}
        if indexed != set(expected)|{'submission-manifest.json'}:
            errors.append('Git index differs from manifest: '+', '.join(sorted((set(expected)|{'submission-manifest.json'})-indexed)))
    if errors:raise ValueError('\n'.join(errors))
    return dict(passed=True, files=len(expected), bytes=sum(r['bytes'] for r in expected.values()),
                secrets_found=False, model_weights_in_git=False, runtime_in_git=False,
                git_index_verified=indexed is not None, private_repository_required=True, private_data_included=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',nargs='?',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    print(json.dumps(verify(args.root),ensure_ascii=False))
