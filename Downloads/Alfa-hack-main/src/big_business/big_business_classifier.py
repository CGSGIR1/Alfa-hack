"""
Определение «крупного бизнеса» для кейса «Новостная аналитика» (Альфа-Банк).

Одна и та же логика (`decide`) используется:
  * для пакетной разметки всех имеющихся данных (`classify_frame`, CLI);
  * для проверки одного нового клиента (`classify_record`).
Поэтому результат для компании не зависит от того, каким способом её проверили.

Правила (все пороги и списки меняются в `Config`):
  1. Данные некорректны (заглушка, дубль ID, нет или неположительная выручка) -> INVALID_DATA.
  2. Выручка > порога (по умолчанию 15 млрд ₽, строго «свыше») -> BIG,
     а если есть флаги «проверить» (низкая численность, госсобственность) -> BIG_REVIEW.
     Флаги из `exclude_flags` (по умолчанию пусто) делают компанию EXCLUDED.
  3. Собственная выручка <= порога, но суммарная выручка группы > порога,
     в группе не меньше `group_min_size` юрлиц, а выручка самой компании
     не ниже `group_member_min_revenue` -> BIG_VIA_GROUP.
  4. Выручка в диапазоне (borderline_low; порог] -> BORDERLINE.
  5. Остальные -> NOT_BIG.

`in_category` = категория входит в `include_categories` (по умолчанию BIG и BIG_REVIEW).
Какие именно исключения называли на Q&A хакатона, мне неизвестно, поэтому флаги
только предупреждают; жёсткими исключениями их делает настройка `exclude_flags`.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import pandas as pd

RULES_VERSION = "1.0"

BIG = "BIG"
BIG_REVIEW = "BIG_REVIEW"
BIG_VIA_GROUP = "BIG_VIA_GROUP"
BORDERLINE = "BORDERLINE"
NOT_BIG = "NOT_BIG"
EXCLUDED = "EXCLUDED"
INVALID_DATA = "INVALID_DATA"

FLAG_LOW_STAFF = "low_staff"                    # большая выручка при численности «Менее 15»
FLAG_STATE_OWNED = "state_owned"                # государственная/муниципальная собственность
FLAG_FOREIGN = "foreign_owned"                  # иностранное участие (только информация)
FLAG_INTERMEDIARY = "possible_intermediary"     # оптовая торговля + низкая численность

FLAG_TITLES = {
    FLAG_LOW_STAFF: "большая выручка при численности меньше 15",
    FLAG_STATE_OWNED: "госсобственность",
    FLAG_FOREIGN: "иностранное участие",
    FLAG_INTERMEDIARY: "возможный посредник (опт при малой численности)",
}


@dataclass(frozen=True)
class Columns:
    """Названия колонок во входных данных / в таблице клиентов."""
    id: str = "SYNTHETIC_U"
    revenue: str = "REVENUE_COMPANY"
    group: str = "GROUP_ID"
    workers: str = "AMOUNT_WORKERS_FNS_GRADATION"
    ownership: str = "OKFS_NAME"
    okved: str = "MAINOKVED_CODE"

    def all(self) -> list[str]:
        return [self.id, self.revenue, self.group, self.workers, self.ownership, self.okved]


@dataclass(frozen=True)
class Config:
    revenue_threshold: float = 15e9
    borderline_low: float = 10e9
    group_min_size: int = 2
    group_member_min_revenue: float = 2e9
    flag_min_revenue: float = 2e9
    low_staff_labels: tuple = ("менее 15",)
    state_keywords: tuple = ("ФЕДЕРАЛЬН", "СУБЪЕКТ", "МУНИЦИПАЛ")
    foreign_keywords: tuple = ("ИНОСТРАН",)
    wholesale_okved_prefix: str = "46"
    review_flags: frozenset = frozenset({FLAG_LOW_STAFF, FLAG_STATE_OWNED})
    exclude_flags: frozenset = frozenset()
    include_categories: frozenset = frozenset({BIG, BIG_REVIEW})
    placeholder_ids: frozenset = frozenset({"U_428697"})
    cols: Columns = Columns()


@dataclass(frozen=True)
class GroupInfo:
    """Сводка по группе: число валидных юрлиц и их суммарная выручка (включая саму компанию)."""
    size: int
    revenue_sum: float


# ---------------------------------------------------------------- вспомогательное

def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _is_blank(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or str(x).strip() == ""


def record_problem(rec: Mapping[str, Any], cfg: Config = Config(), dup: bool = False) -> Optional[str]:
    """Причина, по которой запись нельзя классифицировать, либо None."""
    c = cfg.cols
    cid = rec.get(c.id)
    if _is_blank(cid):
        return "нет идентификатора компании"
    if str(cid) in cfg.placeholder_ids:
        return "идентификатор-заглушка"
    if dup:
        return "дубль идентификатора"
    rev = _to_float(rec.get(c.revenue))
    if rev is None:
        return "выручка не указана или не число"
    if rev <= 0:
        return "выручка не положительная"
    return None


def compute_flags(rec: Mapping[str, Any], revenue: float, cfg: Config = Config()) -> list[str]:
    """Флаги считаются только для компаний с заметной выручкой, чтобы не шуметь на мелких."""
    if revenue < cfg.flag_min_revenue:
        return []
    c = cfg.cols
    flags: list[str] = []
    workers = "" if _is_blank(rec.get(c.workers)) else str(rec.get(c.workers)).strip().lower()
    own = "" if _is_blank(rec.get(c.ownership)) else str(rec.get(c.ownership)).upper()
    okved = "" if _is_blank(rec.get(c.okved)) else str(rec.get(c.okved)).strip()
    low = workers in cfg.low_staff_labels
    if low:
        flags.append(FLAG_LOW_STAFF)
    if any(k in own for k in cfg.state_keywords):
        flags.append(FLAG_STATE_OWNED)
    if any(k in own for k in cfg.foreign_keywords):
        flags.append(FLAG_FOREIGN)
    if low and okved.startswith(cfg.wholesale_okved_prefix):
        flags.append(FLAG_INTERMEDIARY)
    return flags


def _fmt_bln(x: float) -> str:
    return f"{x / 1e9:.2f}".rstrip("0").rstrip(".") + " млрд"


def _result(category: str, cfg: Config, reasons: str, flags: list[str] | None = None,
            group: Optional[GroupInfo] = None, group_checked: bool = False) -> dict:
    return {
        "category": category,
        "in_category": int(category in cfg.include_categories),
        "flags": ";".join(flags or []),
        "reasons": reasons,
        "group_checked": int(group_checked),
        "group_size": group.size if group else None,
        "group_revenue": group.revenue_sum if group else None,
        "rules_version": RULES_VERSION,
    }


# ---------------------------------------------------------------- ядро правил

def decide(rec: Mapping[str, Any], cfg: Config = Config(),
           group: Optional[GroupInfo] = None, dup: bool = False) -> dict:
    """Единственное место, где принимается решение. Остальные функции вызывают его."""
    problem = record_problem(rec, cfg, dup=dup)
    if problem:
        return _result(INVALID_DATA, cfg, problem)

    rev = float(rec[cfg.cols.revenue])
    flags = compute_flags(rec, rev, cfg)
    gc = group is not None
    flag_text = ("; флаги: " + ", ".join(FLAG_TITLES[f] for f in flags)) if flags else ""

    if rev > cfg.revenue_threshold:
        base = f"выручка {_fmt_bln(rev)} > порога {_fmt_bln(cfg.revenue_threshold)}"
        hit_excl = [f for f in flags if f in cfg.exclude_flags]
        if hit_excl:
            return _result(EXCLUDED, cfg, base + flag_text + " (флаг исключения)", flags, group, gc)
        if any(f in cfg.review_flags for f in flags):
            return _result(BIG_REVIEW, cfg, base + flag_text + ": нужна ручная проверка", flags, group, gc)
        return _result(BIG, cfg, base + flag_text, flags, group, gc)

    if (group is not None and group.size >= cfg.group_min_size
            and group.revenue_sum > cfg.revenue_threshold
            and rev >= cfg.group_member_min_revenue):
        return _result(
            BIG_VIA_GROUP, cfg,
            f"своя выручка {_fmt_bln(rev)} ≤ порога, но выручка группы {_fmt_bln(group.revenue_sum)} "
            f"({group.size} юрлиц) > порога" + flag_text, flags, group, gc)

    if rev > cfg.borderline_low:
        return _result(BORDERLINE, cfg, f"выручка {_fmt_bln(rev)} в диапазоне "
                       f"{_fmt_bln(cfg.borderline_low)}–{_fmt_bln(cfg.revenue_threshold)}" + flag_text,
                       flags, group, gc)

    return _result(NOT_BIG, cfg, f"выручка {_fmt_bln(rev)} ≤ порога" + flag_text, flags, group, gc)


# ---------------------------------------------------------------- публичный API

def classify_record(rec: Mapping[str, Any], cfg: Config = Config(),
                    group: Optional[GroupInfo] = None) -> dict:
    """Проверка одного клиента. Если `group` не передан, группа не учитывается (group_checked = 0)."""
    return decide(rec, cfg, group)


def classify_frame(df: pd.DataFrame, cfg: Config = Config()) -> pd.DataFrame:
    """Пакетная разметка. Возвращает таблицу меток с тем же индексом, что у `df`."""
    c = cfg.cols
    missing = [x for x in c.all() if x not in df.columns]
    if missing:
        raise KeyError(f"во входных данных нет колонок: {missing}")

    ids = df[c.id]
    rev = pd.to_numeric(df[c.revenue], errors="coerce")
    dup = ids.duplicated(keep=False) & ids.notna()
    bad = (ids.isna() | ids.astype(str).isin(cfg.placeholder_ids) | dup | rev.isna() | (rev <= 0))

    # в суммы по группам попадают только корректные строки
    gdf = pd.DataFrame({"g": df[c.group], "rev": rev})[~bad & df[c.group].notna()]
    stats = gdf.groupby("g")["rev"].agg(size="count", revenue_sum="sum")
    gmap = {g: GroupInfo(int(n), float(s))
            for g, n, s in zip(stats.index, stats["size"], stats["revenue_sum"])}

    out = []
    for rec, d in zip(df[c.all()].to_dict("records"), dup.tolist()):
        g = gmap.get(rec[c.group]) if not _is_blank(rec[c.group]) else None
        out.append(decide(rec, cfg, g, dup=d))
    res = pd.DataFrame(out, index=df.index)
    res.insert(0, c.id, df[c.id])
    return res


# ---------------------------------------------------------------- командная строка

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Разметка компаний: попадают ли в категорию «крупный бизнес»")
    p.add_argument("input", help="CSV с данными о компаниях")
    p.add_argument("-o", "--output", default="clients_labeled.csv")
    p.add_argument("--sep", default=";")
    p.add_argument("--threshold", type=float, default=Config.revenue_threshold, help="порог выручки, ₽")
    p.add_argument("--full", action="store_true", help="сохранить все исходные колонки вместе с метками")
    a = p.parse_args(argv)

    cfg = Config(revenue_threshold=a.threshold)
    df = pd.read_csv(a.input, sep=a.sep, dtype={cfg.cols.okved: str})
    labels = classify_frame(df, cfg)
    base = df if a.full else df[[cfg.cols.id, cfg.cols.revenue, cfg.cols.group]]
    out = pd.concat([base, labels.drop(columns=[cfg.cols.id])], axis=1)
    out.to_csv(a.output, sep=a.sep, index=False, encoding="utf-8-sig")
    print(labels.category.value_counts().to_string())
    print(f"\nin_category = 1: {int(labels.in_category.sum())} из {len(labels)} -> {a.output}")


if __name__ == "__main__":
    main()
