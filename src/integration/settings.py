from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
from uuid import uuid4

from news_analytics.common import read_json
from news_analytics.retrieval import Settings

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ENV_FILE = ROOT / '.env'


def path(value):
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()


def load_env(env_file=None):
    # Never put credentials in JSON, reports or the container image.
    target = path(env_file or os.environ.get('LLM_ENV_FILE') or '.env')
    global ACTIVE_ENV_FILE
    ACTIVE_ENV_FILE = target
    if target.is_file():
        for line in target.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


def save_auto_poll(enabled):
    """Change only the automatic-processing flag, never expose other .env values."""
    target = ACTIVE_ENV_FILE
    text = target.read_text(encoding='utf-8-sig') if target.is_file() else ''
    line = 'PIPELINE_AUTO_POLL=' + ('1' if enabled else '0')
    if re.search(r'^\s*PIPELINE_AUTO_POLL\s*=', text, re.M):
        text = re.sub(r'^\s*PIPELINE_AUTO_POLL\s*=.*$', line, text, flags=re.M)
    else:
        text = text.rstrip('\r\n') + '\n' + line + '\n'
    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    os.environ['PIPELINE_AUTO_POLL'] = '1' if enabled else '0'


def load(config=None):
    cfg = read_json(path(config or 'config.json'))
    for key in ('database', 'model_path', 'reports', 'parser_directory', 'prepared_news'):
        cfg[key] = path(cfg[key])
    cfg['model_path'] = path(os.environ.get('EMBEDDING_MODEL_PATH') or cfg['model_path'])
    for key in ('inputs', 'upload_folders'):
        cfg[key] = {k: path(v) for k, v in cfg.get(key, {}).items()}
    return cfg


def retrieval(cfg, as_of=None, top_news=None):
    names = {f.name for f in fields(Settings)}
    values = {k: v for k, v in cfg.get('retrieval', {}).items() if k in names}
    values['as_of'] = as_of or datetime.now(timezone(timedelta(hours=3))).date().isoformat()
    if top_news is not None:
        values['top_news'] = top_news
    return Settings(**values).validate()


def llm_ready():
    return all(os.environ.get(k, '').strip() for k in ('LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL_MAIN'))
