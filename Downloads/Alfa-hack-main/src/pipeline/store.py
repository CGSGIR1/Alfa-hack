"""Хранилище пайплайна (SQLite): компании, новости, показатели, отчёты.

Большие структуры (профиль, аспекты, разметка новости, отчёт) хранятся как JSON-колонки:
схема прототипа меняется часто, а запросы к ним простые.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import APP_DB

SCHEMA = """
create table if not exists companies (
    id              text primary key,         -- SYNTHETIC_U или GROUP_ID
    subject_type    text not null,            -- company | group
    description     text not null,            -- шаблонный текст
    facts           text not null,            -- JSON: факты для пакета
    limitations     text not null,            -- JSON: ограничения данных
    aspects         text not null,            -- JSON: аспекты влияния (QuerySet)
    aspects_origin  text not null,            -- llm | rules
    tracked         integer not null default 1,
    created_at      text not null,
    updated_at      text not null,
    embedded_at     text
);
create table if not exists clients (            -- клиенты банка (из CSV кейса) для классификатора крупного бизнеса
    SYNTHETIC_U                  text,
    REVENUE_COMPANY              real,
    GROUP_ID                     text,
    AMOUNT_WORKERS_FNS_GRADATION text,
    OKFS_NAME                    text,
    MAINOKVED_CODE               text
);
create index if not exists idx_clients_id on clients(SYNTHETIC_U);
create index if not exists idx_clients_group on clients(GROUP_ID);
create table if not exists client_category (    -- результат классификатора (src/big_business/db_hook.py)
    company_id    text primary key,
    category      text not null,
    in_category   integer not null,             -- 1: пользователь пайплайна (отчёты и переключатель)
    flags         text,
    reasons       text,
    group_size    integer,
    group_revenue double precision,
    rules_version text not null,
    classified_at text not null
);
create table if not exists news (
    id              text primary key,         -- id новости парсера или indicator:<код>:<дата>
    kind            text not null,            -- article | indicator
    event_id        text,
    published_utc   text not null,
    title           text not null,
    url             text,
    publisher       text,
    importance      text not null,            -- high | normal | low
    expires_at      text not null,
    prepared        text not null,            -- JSON: news-two-views-1.0
    prepared_at     text not null,
    embedded_at     text
);
create index if not exists idx_news_published on news(published_utc);
create index if not exists idx_news_expires on news(expires_at);
create table if not exists news_expired (   -- новости, удалённые по сроку: не принимать их снова как новые
    id              text primary key,
    published_utc   text not null,
    expired_at      text not null
);
create index if not exists idx_news_expired_published on news_expired(published_utc);
create table if not exists news_failed (    -- новости, которые не удалось разметить: когда пробовать снова
    id              text primary key,
    published_utc   text not null,
    attempts        integer not null,
    last_error      text not null,
    last_attempt_at text not null,
    retry_after     text                    -- null: попытки исчерпаны, новость больше не берём
);
create index if not exists idx_news_failed_published on news_failed(published_utc);
create table if not exists indicators (
    code            text not null,
    date            text not null,
    value           real not null,
    source          text not null,
    fetched_at      text not null,
    primary key (code, date)
);
create table if not exists reports (
    company_id      text not null,
    generated_at    text not null,
    reason          text not null,
    top_fragments   text not null,            -- JSON: топ-N фрагментов на момент генерации
    package         text not null,            -- JSON: вход report-generation-input-1.0
    report          text not null,            -- JSON: company-report-1.0
    cost            real,
    primary key (company_id, generated_at)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path = APP_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str | None):
    return json.loads(text) if text else None
