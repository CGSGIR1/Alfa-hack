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
