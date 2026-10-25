"""
Подключение классификатора к базе клиентов.

Работает с любой БД, у которой есть DB-API-драйвер (sqlite3, psycopg2, pymysql ...).
Предполагается, что таблица клиентов содержит колонки из `Columns`
(по умолчанию как в датасете хакатона). Результат пишется в отдельную таблицу `client_category`,
чтобы не менять основную таблицу клиентов.

Как использовать
  svc = CategoryService(conn, placeholder="?")      # для psycopg2/pymysql: placeholder="%s"
  svc.ensure_schema()
  svc.backfill()                                    # один раз: разметить всех имеющихся клиентов
  ...
  svc.classify_company("U_123")                     # сразу после добавления/изменения клиента
  svc.classify_pending()                            # или по расписанию: добирает всех без метки

Важно: новый клиент меняет суммарную выручку своей группы, поэтому `classify_company`
пересчитывает всю группу, а не одну компанию. Иначе уже размеченные участники группы
остались бы со старой меткой.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .big_business_classifier import (Config, GroupInfo, RULES_VERSION, classify_frame,
                                      decide, record_problem, _is_blank)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"недопустимое имя в SQL: {name!r}")
    return name


class CategoryService:
    def __init__(self, conn, clients_table: str = "clients", result_table: str = "client_category",
                 cfg: Config = Config(), placeholder: str = "?"):
        self.conn = conn
        self.cfg = cfg
        self.t = _ident(clients_table)
        self.r = _ident(result_table)
        self.ph = placeholder
        self.cols = [_ident(x) for x in cfg.cols.all()]

    # ------------------------------------------------------------------ схема
    def ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.r} (
                company_id    TEXT PRIMARY KEY,
                category      TEXT NOT NULL,
                in_category   INTEGER NOT NULL,
                flags         TEXT,
                reasons       TEXT,
                group_size    INTEGER,
                group_revenue DOUBLE PRECISION,
                rules_version TEXT NOT NULL,
                classified_at TEXT NOT NULL
            )""")
        self.conn.commit()

    # ------------------------------------------------------------------ чтение
    def _fetch(self, where: str, params: tuple) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT {', '.join(self.cols)} FROM {self.t} WHERE {where}", params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def _group_info(self, members: list[dict]) -> GroupInfo:
        c = self.cfg.cols
        ok = [m for m in members if record_problem(m, self.cfg) is None]
        return GroupInfo(len(ok), float(sum(float(m[c.revenue]) for m in ok)))

    # ------------------------------------------------------------------ запись
    def _upsert(self, rows: list[tuple]) -> None:
        ph = ", ".join([self.ph] * 9)
        sql = (f"INSERT INTO {self.r} (company_id, category, in_category, flags, reasons, group_size, "
               f"group_revenue, rules_version, classified_at) VALUES ({ph}) "
               f"ON CONFLICT (company_id) DO UPDATE SET category = excluded.category, "
               f"in_category = excluded.in_category, flags = excluded.flags, reasons = excluded.reasons, "
               f"group_size = excluded.group_size, group_revenue = excluded.group_revenue, "
               f"rules_version = excluded.rules_version, classified_at = excluded.classified_at")
        cur = self.conn.cursor()
        cur.executemany(sql, rows)
        self.conn.commit()

    @staticmethod
    def _row(cid: str, res: dict, now: str) -> tuple:
        return (str(cid), res["category"], res["in_category"], res["flags"], res["reasons"],
                res["group_size"], res["group_revenue"], res["rules_version"], now)

    # ------------------------------------------------------------------ основные операции
    def classify_company(self, company_id: str, old_group_id: Optional[str] = None) -> dict:
        """Проверить клиента и пересчитать его группу. Вызывать сразу после вставки/изменения.

        Если клиент сменил группу, передайте прежнюю в `old_group_id`, она тоже будет пересчитана.
        Возвращает результат для самой компании."""
        c = self.cfg.cols
        me = self._fetch(f"{c.id} = {self.ph}", (company_id,))
        if not me:
            raise KeyError(f"клиент {company_id!r} не найден в таблице {self.t}")
        me = me[0]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows, mine = [], None

        gid = me[c.group]
        batches: list[tuple[Optional[str], list[dict]]] = []
        if _is_blank(gid):
            batches.append((None, [me]))
        else:
            batches.append((gid, self._fetch(f"{c.group} = {self.ph}", (gid,))))
        if not _is_blank(old_group_id) and old_group_id != gid:
            batches.append((old_group_id, self._fetch(f"{c.group} = {self.ph}", (old_group_id,))))

        for g, members in batches:
            info = self._group_info(members) if g is not None else None
            for m in members:
                res = decide(m, self.cfg, info)
                rows.append(self._row(m[c.id], res, now))
                if m[c.id] == company_id:
                    mine = res
        self._upsert(rows)
        return mine

    def classify_pending(self) -> int:
        """Разметить всех клиентов, у кого ещё нет метки (или метка по старой версии правил)."""
        c = self.cfg.cols
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT k.{c.id} FROM {self.t} k LEFT JOIN {self.r} r ON r.company_id = k.{c.id} "
            f"WHERE r.company_id IS NULL OR r.rules_version <> {self.ph}", (RULES_VERSION,))
        ids = [r[0] for r in cur.fetchall()]
        done: set = set()
        for cid in ids:
            if cid in done:
                continue
            self.classify_company(cid)
            done.add(cid)
        return len(ids)

    def backfill(self) -> int:
        """Разметить всю таблицу клиентов разом (быстрее, чем по одному). Возвращает число строк."""
        df = pd.read_sql_query(f"SELECT {', '.join(self.cols)} FROM {self.t}", self.conn)
        labels = classify_frame(df, self.cfg)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c = self.cfg.cols
        rows = []
        for cid, rec in zip(labels[c.id], labels.to_dict("records")):
            rows.append(self._row(cid, rec, now))
        # дубли ID в основной таблице получат одну запись (последнюю): это ошибка данных, она видна в reasons
        self._upsert(rows)
        return len(rows)

    def get(self, company_id: str) -> Optional[dict]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM {self.r} WHERE company_id = {self.ph}", (company_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))
