"""Make a clean, staged Git copy and a source ZIP; never commit or push."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from datetime import datetime,timezone

from verify_submission import verify

ROOT=Path(__file__).resolve().parents[1]
FILES=('README.md','THIRD_PARTY_NOTICES.md','.env.example','.gitignore','.gitattributes','.dockerignore',
       'Dockerfile','Dockerfile.with-model','compose.yaml','compose.bundled.yaml','config.json','app.py',
       'requirements.txt','requirements.lock','requirements-test.txt','start_interface.cmd',
       'start_management.cmd','stop_interface.cmd','restart_interface.cmd','design/reference.html')
TREES=('src','tests','docs','.github','company-news-analytics','private-data')
TOOLS=('launch_interface.py','restart_interface.ps1','prepare_model_context.py','smoke_model.py',
       'verify_submission.py','prepare_submission.py','verify_public_image.py','validate_private_data.py')
SKIP={'.git','.venv','__pycache__','.cache','models','archive','experiments','backups','import-logs','reports',
      'companies-uploaded','news-uploaded','analytics-uploaded'}
SUFFIXES={'.db','.sqlite','.sqlite3','.log','.lock','.tmp','.csv','.safetensors','.gguf','.onnx','.pt','.pth','.bin','.model','.zip'}
BINARY={'.pdf','.csv','.mp4','.png','.webp','.ico','.npz'}


def run(*args,**kwargs):
    return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs).stdout


def secret():
    target=ROOT/'.env'
    if target.is_file():
        for line in target.read_text(encoding='utf-8-sig').splitlines():
            if line.strip().startswith('LLM_API_KEY='):
                value=line.split('=',1)[1].strip().strip('\"').strip("'")
                return value.encode() if value else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT):parser.error('Use a new directory outside the working project')
    selected={ROOT/name for name in FILES}|{ROOT/'tools'/name for name in TOOLS}
    for name in TREES:
        for path in (ROOT/name).rglob('*'):
            rel=path.relative_to(ROOT)
            if not path.is_file() or any(p in SKIP for p in rel.parts):continue
            if (path.suffix.lower() in SUFFIXES and not (rel.parts[0]=='private-data' and path.suffix.lower()=='.csv')) or path.suffix.lower()=='.pdf' or '.db-' in path.name:continue
            if path.name.startswith('.env') and path.name!='.env.example':continue
            selected.add(path)
    key=secret()
    # Check the actual current key in the inherited Git history before cloning it.
    commits=run('git','rev-list','--all',cwd=ROOT).decode().splitlines()
    if key:
        for commit in commits:
            result=subprocess.run(['git','grep','-I','-l','-F','-f','-',commit,'--','.'],
                cwd=ROOT,input=key+b'\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            if result.returncode not in (0,1):raise RuntimeError('Could not scan Git history')
            if result.returncode==0:raise ValueError('Current API key is present in inherited Git history; rotate before delivery')
    output.mkdir(parents=True)
    target=output/'github'
    run('git','clone','--quiet','--no-checkout','--no-hardlinks',str(ROOT),str(target))
    run('git','remote','set-url','origin','https://github.com/CGSGIR1/Alfa-hack.git',cwd=target)
    rows=[]
    for source in sorted(selected):
        if not source.resolve().is_relative_to(ROOT):raise ValueError('Source points outside project')
        rel=source.relative_to(ROOT)
        body=source.read_bytes()
        if source.suffix.lower() not in BINARY:
            body=body.replace(b'\r\n',b'\n')
            if source.suffix.lower()=='.cmd':body=body.replace(b'\n',b'\r\n')
        destination=target/rel;destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(body)
        rows.append(dict(path=rel.as_posix(),bytes=len(body),sha256=hashlib.sha256(body).hexdigest()))
    manifest=dict(version='1.1.0',created_at=datetime.now(timezone.utc).isoformat(),
                  source_commit=run('git','rev-parse','HEAD',cwd=ROOT).decode().strip(),
                  history_key_scan_commits=len(commits),files=rows)
    (target/'submission-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    verify(target,key,git_index=False)
    run('git','add','--all',cwd=target)
    report=verify(target,key)
    staged=run('git','diff','--cached','--numstat',cwd=target).decode()
    (output/'git-staged-files.txt').write_text(staged,encoding='utf-8')
    with zipfile.ZipFile(output/'alfa-hack-1.1.0-private-source.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for name in [r['path'] for r in rows]+['submission-manifest.json']:
            archive.write(target/name,'alfa-hack/'+name)
    (output/'source-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(directory=str(output),verification=report,git_committed=False,git_pushed=False),ensure_ascii=False))


if __name__=='__main__':main()
