"""Reversible report archival without resetting completed news comparisons."""
from datetime import datetime,timezone
from pathlib import Path
import json,shutil,sqlite3

from news_analytics.common import require
from news_analytics.database import connect,check
from news_analytics.workflow import workflow_lock


def archive_reports(db,reports):
    db=Path(db).resolve();reports=Path(reports).resolve()
    require(reports.is_dir(),'Report directory does not exist')
    require(reports.is_relative_to(db.parent),'Reports must be inside the application runtime')
    parent=(db.parent/'reports-archive').resolve()
    require(not parent.is_relative_to(reports),'Archive must be outside the active report directory')
    folder=parent/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    require(folder.resolve().parent==parent,'Unexpected archive target')
    with workflow_lock(db.parent/'integration'),workflow_lock(db):
        folder.mkdir(parents=True,exist_ok=False)
        archived=folder/'reports';archived.mkdir()
        paths=list(reports.iterdir())
        for path in paths:
            require(not path.is_symlink() and path.resolve().parent==reports,'Unexpected report path: '+str(path))
            require((archived/path.name).resolve().parent==archived.resolve(),'Unexpected archive path')
        with connect(db,True) as source:
            target=sqlite3.connect(str(folder/'database-before.db'))
            try:source.backup(target)
            finally:target.close()
        moved=[]
        with connect(db) as c:
            try:
                c.execute('BEGIN IMMEDIATE')
                c.execute('''CREATE TABLE IF NOT EXISTS archived_report_batches (
                  run_id TEXT NOT NULL,company_id TEXT NOT NULL,archived_at TEXT NOT NULL,
                  archive_path TEXT NOT NULL,original_status TEXT NOT NULL,
                  PRIMARY KEY(run_id,company_id),
                  FOREIGN KEY(run_id,company_id) REFERENCES company_report_batches(run_id,company_id))''')
                rows=c.execute('''SELECT b.* FROM company_report_batches b WHERE NOT EXISTS
                  (SELECT 1 FROM archived_report_batches a WHERE a.run_id=b.run_id AND a.company_id=b.company_id)''').fetchall()
                stamp=datetime.now(timezone.utc).isoformat()
                for path in paths:
                    destination=archived/path.name
                    shutil.move(str(path),str(destination));moved.append((path,destination))
                for row in rows:
                    c.execute('INSERT INTO archived_report_batches VALUES(?,?,?,?,?)',
                        (row['run_id'],row['company_id'],stamp,str(folder),row['status']))
                    saved=json.loads(row['saved_json']);old=Path(saved['output']).resolve()
                    if old.is_relative_to(reports):
                        saved['output']=str(archived/old.relative_to(reports))
                        c.execute('UPDATE company_report_batches SET saved_json=? WHERE run_id=? AND company_id=?',
                            (json.dumps(saved,ensure_ascii=False),row['run_id'],row['company_id']))
                    if row['status']=='prepared':
                        c.execute("UPDATE company_report_batches SET status='cancelled',last_error='Report moved to archive by user' WHERE run_id=? AND company_id=?",
                            (row['run_id'],row['company_id']))
                check(c);c.commit()
            except Exception:
                c.rollback()
                for old,destination in reversed(moved):shutil.move(str(destination),str(old))
                raise
        result=dict(archive=str(folder),report_batches=len(rows),moved_entries=len(paths),
            active_directory=str(reports),remaining_entries=len(list(reports.iterdir())),
            comparison_memory_preserved=True)
        (folder/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
