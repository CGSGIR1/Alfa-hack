from __future__ import annotations
from contextlib import contextmanager,closing
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
from .common import require

@contextmanager
def connect(path,readonly=False):
    path=Path(path).resolve()
    require(path.is_file(),f'Database not found: {path}')
    c=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=30)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    try:yield c
    finally:c.close()

def initialize(path):
    path=Path(path).resolve();path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(not tables or 'schema_version' in tables,'This is a legacy database. Use a new DB path; no legacy tables will be overwritten.')
        if tables:require(c.execute('SELECT version FROM schema_version').fetchall()==[(1,)],'Unsupported database version')
        c.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        c.commit()
    return path

def backup(path):
    path=Path(path);folder=path.parent/'backups';folder.mkdir(parents=True,exist_ok=True)
    target=folder/f'{path.stem}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db'
    with connect(path,True) as src,closing(sqlite3.connect(target)) as dest:src.backup(dest)
    return str(target)

def check(c):
    require(c.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Database integrity check failed')
    require(not c.execute('PRAGMA foreign_key_check').fetchall(),'Foreign key check failed')
