from __future__ import annotations
import hashlib,json,math,os
from datetime import datetime,timezone
from pathlib import Path

_source_root=Path(__file__).resolve().parent.parent
ROOT=Path(os.environ['NEWS_ANALYTICS_HOME']).resolve() if os.environ.get('NEWS_ANALYTICS_HOME') else (
    _source_root if (_source_root/'config.json').is_file() else Path.cwd())
def require(ok,message):
    if not ok:raise ValueError(message)
def text(value,label):
    require(isinstance(value,str) and bool(value.strip()),f'{label}: nonempty string required')
    return value
def dumps(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def read_json(path):
    def pairs(items):
        result={}
        for k,v in items:
            require(k not in result,f'Duplicate JSON key: {k}');result[k]=v
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,
                      parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Invalid number: '+x)))
def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(path)
def number(value,label,minimum=0):
    require(type(value) in (int,float) and math.isfinite(value) and value>=minimum,f'{label}: invalid number')
    return value
