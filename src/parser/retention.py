"""Parser retention helpers; no network calls or project-model dependencies."""
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import csv
import os
import sqlite3


def take_storage_lock(path):
    stream=open(path,'ab')
    try:
        if os.name=='nt':
            import msvcrt
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close();return None
    return stream


def prune_parser(conn,cutoff,dry_run=False):
    # Keep unknown/invalid dates for inspection, rather than guessing their age.
    ids=[r[0] for r in conn.execute('SELECT id FROM news WHERE date(published_utc) IS NOT NULL AND substr(published_utc,1,10)<?',(cutoff,))]
    backup=None
    if ids and not dry_run:
        name=conn.execute('PRAGMA database_list').fetchone()[2]
        if name:
            source=Path(name);folder=source.parent/'backups';folder.mkdir(exist_ok=True)
            target=folder/(source.stem+'.before-retention.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.db')
            with closing(sqlite3.connect(target)) as dest:conn.backup(dest)
            backup=str(target)
        with conn:conn.executemany('DELETE FROM news WHERE id=?',[(x,) for x in ids])
    return dict(deleted=len(ids),source_ids=ids,backup=backup,dry_run=dry_run,cutoff=cutoff)


def export_parser_csv(conn,csv_path):
    csv_path=Path(csv_path);temp=csv_path.with_name(csv_path.name+'.tmp')
    try:
        cur=conn.execute('SELECT * FROM news')
        with temp.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream,delimiter=';');writer.writerow([d[0] for d in cur.description]);writer.writerows(cur)
        temp.replace(csv_path)
    finally:temp.unlink(missing_ok=True)


def cleanup_parser(db,cutoff,dry_run=False):
    db=Path(db)
    if not db.is_file():return dict(deleted=0,source_ids=[],backup=None,dry_run=dry_run,cutoff=cutoff)
    lock=take_storage_lock(db.parent/'parser.lock')
    if lock is None:return dict(deleted=0,source_ids=[],deferred='parser_busy',cutoff=cutoff)
    with lock,closing(sqlite3.connect(db,timeout=15)) as conn:
        result=prune_parser(conn,cutoff,dry_run)
        if result['deleted'] and not dry_run:
            export_parser_csv(conn,db.with_suffix('.csv'))
        return result
