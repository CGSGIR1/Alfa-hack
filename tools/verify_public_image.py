"""Scan every saved Docker/OCI layer for organizer data and credential formats."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

SECRET = re.compile(rb'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{24,}|sk-(?:proj-)?[A-Za-z0-9_-]{35,})\b')


def private_signatures(root):
    fingerprints, snippets = set(), set()
    for item in (root/'private-data').rglob('*'):
        if not item.is_file() or item.suffix not in {'.csv', '.json'}:
            continue
        body = item.read_bytes()
        fingerprints.add(hashlib.sha256(body).hexdigest())
        if item.suffix == '.json' and item.parent.name in {'companies', 'analytics', 'news'}:
            value = json.loads(body)
            if item.parent.name == 'companies':
                texts = [value['profile_text']] + [q['text'] for q in value['queries']]
            elif item.parent.name == 'analytics':
                texts = [p['text'] for p in value['source_pages']] + [m['embedding_text'] for m in value['mechanisms']]
            else:
                texts = [f['company_embedding_text'] for f in value['fragments']]
            for text in texts:
                if len(text) >= 180:
                    # Raw UTF-8 and JSON-escaped text, for accidental embedded representations.
                    text = text[:220]
                    snippets.update((text.encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode(),
                                     json.dumps(text, ensure_ascii=True)[1:-1].encode()))
    return fingerprints, snippets


def verify(archive, root, secrets_env=None):
    fingerprints, snippets = private_signatures(root)
    exact_secrets = []
    if secrets_env:
        for line in secrets_env.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                value = value.strip().strip('\"').strip("'")
                if ('API_KEY' in key or 'TOKEN' in key) and len(value) >= 12:
                    exact_secrets.append(value.encode())
    problems, layers, count, text_count = [], 0, 0, 0
    with tarfile.open(archive, 'r:*') as outer:
        members = outer.getmembers()
        byname = {m.name:m for m in members}
        for member in members:
            if member.isfile() and member.size < 2_000_000:
                content = outer.extractfile(member).read()
                if SECRET.search(content) or any(value in content for value in exact_secrets):
                    problems.append('Credential in image metadata: '+member.name)
        if 'manifest.json' in byname:
            manifests = json.load(outer.extractfile(byname['manifest.json']))
            names = {name for m in manifests for name in m['Layers']}
        else:
            # OCI exports store media-type descriptors in JSON blobs/index.json.
            names = set()
            for member in members:
                if member.isfile() and member.size < 2_000_000:
                    content = outer.extractfile(member).read()
                    try:
                        value = json.loads(content)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        for desc in value.get('layers', []):
                            names.add('blobs/'+desc['digest'].replace(':','/'))
        assert names, 'No image layers found'
        for name in sorted(names):
            stream = outer.extractfile(byname[name])
            with tarfile.open(fileobj=stream, mode='r|*') as layer:
                layers += 1
                for entry in layer:
                    if not entry.isfile():
                        continue
                    count += 1
                    path = PurePosixPath(entry.name)
                    application = path.parts and path.parts[0] == 'app'
                    if application and (any(p in {'private-data','data','examples'} for p in path.parts)
                            or path.suffix.lower() in {'.pdf','.csv','.db','.sqlite','.sqlite3'}
                            or (path.name.startswith('.env') and path.name != '.env.example')):
                        problems.append('Forbidden layer path: '+entry.name)
                    handle = layer.extractfile(entry)
                    digest = hashlib.sha256()
                    body = bytearray() if application and entry.size < 12_000_000 else None
                    while block := handle.read(1024*1024):
                        digest.update(block)
                        if body is not None:
                            body.extend(block)
                    if digest.hexdigest() in fingerprints:
                        problems.append('Private file hash in layer: '+entry.name)
                    if body is not None and path.suffix.lower() not in {'.mp4','.webp','.png','.ico','.jpg','.jpeg'}:
                        text_count += 1
                        if SECRET.search(body):
                            problems.append('Credential format in layer: '+entry.name)
                        if any(value in body for value in exact_secrets):
                            problems.append('Exact credential in layer: '+entry.name)
                        if any(text in body for text in snippets):
                            problems.append('Private text in layer: '+entry.name)
    if problems:
        raise ValueError('\n'.join(problems))
    with archive.open('rb') as stream:
        archive_sha256 = hashlib.file_digest(stream,'sha256').hexdigest()
    return dict(passed=True, layers_scanned=layers, files_scanned=count,
                application_text_files_scanned=text_count, private_file_hashes=len(fingerprints),
                private_text_signatures=len(snippets), private_data_found=False,
                credential_formats_found=False, exact_credentials_checked=len(exact_secrets),
                archive_sha256=archive_sha256)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--secrets-env', type=Path, help='Optional local .env for exact credential comparison; values are never printed')
    args = parser.parse_args()
    print(json.dumps(verify(args.archive, args.root, args.secrets_env), ensure_ascii=False))
