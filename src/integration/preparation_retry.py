"""Durable bounded retry policy for preparation; no model or API calls here."""
from datetime import datetime,timedelta,timezone
import os

from news_analytics.common import dumps,sha
from news_analytics.database import connect
from src.llm import LLMResponseError,LLMServiceError


def clock():return datetime.now(timezone.utc)


def signature():
    # Store only a hash; credentials never enter the journal or API state.
    return sha(dumps({key:os.environ.get(key,'') for key in
        ('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL_NEWS','LLM_MODEL_FAST','LLM_MODEL_MAIN')}))


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS news_preparation_retry_state (
      source_id TEXT NOT NULL,source_hash TEXT NOT NULL,prompt_version TEXT NOT NULL,
      error_kind TEXT NOT NULL,next_retry_at TEXT,terminal INTEGER NOT NULL CHECK(terminal IN (0,1)),
      PRIMARY KEY(source_id,source_hash,prompt_version),
      FOREIGN KEY(source_id,source_hash,prompt_version)
        REFERENCES news_preparation_jobs(source_id,source_hash,prompt_version) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS news_preparation_control (
      id INTEGER PRIMARY KEY CHECK(id=1),error_kind TEXT NOT NULL,reason TEXT NOT NULL,
      next_retry_at TEXT,config_signature TEXT NOT NULL)''')
    # Old cached model refusals are already completed failures, not outages.
    c.execute("""INSERT OR IGNORE INTO news_preparation_retry_state
      SELECT source_id,source_hash,prompt_version,'response',NULL,1
      FROM news_preparation_jobs WHERE status='retry' AND
      (last_error LIKE 'Модель отказалась обрабатывать вход:%'
       OR last_error LIKE 'Модель вернула невалидный JSON:%'
       OR last_error LIKE 'Модель не вернула текст ответа%')""")
    c.execute("""UPDATE news_preparation_jobs SET status='failed' WHERE status='retry' AND EXISTS
      (SELECT 1 FROM news_preparation_retry_state s WHERE s.source_id=news_preparation_jobs.source_id
       AND s.source_hash=news_preparation_jobs.source_hash AND s.prompt_version=news_preparation_jobs.prompt_version AND s.terminal=1)""")


def reset(db,prompt_version):
    """Explicit operator retry grants one new bounded round; successes stay intact."""
    with connect(db) as c,c:
        c.execute("DELETE FROM news_preparation_retry_state WHERE prompt_version=?",(prompt_version,))
        c.execute("UPDATE news_preparation_jobs SET status='pending',attempts=0,last_error=NULL WHERE prompt_version=? AND status IN ('failed','retry','blocked')",(prompt_version,))
        c.execute('DELETE FROM news_preparation_control')


def pause(db):
    with connect(db) as c,c:
        row=c.execute('SELECT * FROM news_preparation_control WHERE id=1').fetchone()
        if row is None:return None
        if row['config_signature']==signature() and (row['next_retry_at'] is None or datetime.fromisoformat(row['next_retry_at'])>clock()):
            return dict(error_kind=row['error_kind'],reason=row['reason'],next_retry_at=row['next_retry_at'])
        c.execute('DELETE FROM news_preparation_control')
    return None


def eligibility(db,key,previous,max_attempts):
    """ready, complete, held or cooldown; checking does not spend an attempt."""
    if not previous:return 'ready'
    if previous['status'] in ('prepared','duplicate'):return 'complete'
    with connect(db) as c,c:
        state=c.execute('SELECT * FROM news_preparation_retry_state WHERE source_id=? AND source_hash=? AND prompt_version=?',key).fetchone()
        if state and state['terminal']:return 'held'
        if previous['status'] in ('failed','retry') and previous['attempts']>=max_attempts:
            c.execute("UPDATE news_preparation_jobs SET status='failed' WHERE source_id=? AND source_hash=? AND prompt_version=?",key)
            return 'held'
        if state and state['next_retry_at'] and datetime.fromisoformat(state['next_retry_at'])>clock():return 'cooldown'
    return 'ready'


def record_failure(db,key,error,record,max_attempts,base_delay=60,max_delay=600):
    response=isinstance(error,LLMResponseError) or isinstance(error,(ValueError,KeyError,TypeError,IndexError))
    service=isinstance(error,LLMServiceError)
    access=service and not error.retryable
    with connect(db,True) as c:
        row=c.execute('SELECT attempts FROM news_preparation_jobs WHERE source_id=? AND source_hash=? AND prompt_version=?',key).fetchone()
    attempts=(row[0] if row else 0)+1
    terminal=response or (not access and attempts>=max_attempts)
    kind='response' if response else 'access' if access else 'transport' if service else 'unknown'
    next_at=None if terminal or access else (clock()+timedelta(seconds=min(base_delay*2**min(attempts-1,16),max_delay))).isoformat()
    status='failed' if terminal else 'blocked' if access else 'retry'
    record(key,status,error=str(error))
    with connect(db) as c,c:
        c.execute('''INSERT INTO news_preparation_retry_state VALUES(?,?,?,?,?,?)
          ON CONFLICT(source_id,source_hash,prompt_version) DO UPDATE SET
          error_kind=excluded.error_kind,next_retry_at=excluded.next_retry_at,terminal=excluded.terminal''',
          (*key,kind,next_at,int(terminal)))
        if service:
            # Even an exhausted article must not cause a burst against an unavailable API.
            global_next=None if access else (clock()+timedelta(seconds=min(base_delay*2**min(attempts-1,16),max_delay))).isoformat()
            c.execute('''INSERT INTO news_preparation_control VALUES(1,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET error_kind=excluded.error_kind,reason=excluded.reason,
              next_retry_at=excluded.next_retry_at,config_signature=excluded.config_signature''',
              (kind,str(error),global_next,signature()))
    return dict(source_id=key[0],reason=str(error),error_kind=kind,retry=not terminal and not access,
                terminal=terminal,next_retry_at=next_at,shared_pause=service)
