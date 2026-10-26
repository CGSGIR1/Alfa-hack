"""Рыночные и макропоказатели: сбор, резкие движения с учётом волатильности, блок для пакета.

Источники (официальные, бесплатные):
- Банк России: ключевая ставка, RUONIA, инфляция (ИПЦ г/г), официальные курсы валют,
  учётные цены золота, серебра, платины и палладия;
- Московская биржа (ISS API): биржевой курс юаня CNYRUB_TOM, индексы IMOEX и RGBI,
  нефть Brent (ближайший фьючерс BR), кривая бескупонной доходности ОФЗ (1, 5, 10 лет).

Резкое движение определяется относительно обычной волатильности показателя: недельное изменение
сравнивается с устойчивой оценкой σ недельных изменений за последний год (z-оценка). От 1,5σ —
«заметное» событие, от 2,5σ — «сильное» (только оно запускает внеплановый пересчёт отчётов).
Однонаправленные движения валют, металлов и ОФЗ объединяются в одно событие.
Для ставок изменения считаются в процентных пунктах, для цен — в процентах. Ключевая ставка —
событие при любом изменении, инфляция — по изменению месячного значения г/г.

Событие превращается в документ news-two-views-1.0 по шаблону, без LLM: числа точные,
статус — reported_fact, механизм — шаблонная гипотеза. Такие события проходят тот же поиск
и ту же оценку применимости, что и новости.
"""

from __future__ import annotations

import json
import re
import statistics
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3
from pathlib import Path

from . import store

_DATABASE=ContextVar('indicator_database',default=None)


@contextmanager
def using_database(database):
    """Route indicator access to the integrated DB without legacy table setup."""
    token=_DATABASE.set(database)
    try:yield
    finally:_DATABASE.reset(token)


@contextmanager
def _connect():
    database=_DATABASE.get()
    if database is None:
        with store.connect() as connection:yield connection
        return
    connection=sqlite3.connect(Path(database),timeout=30)
    connection.row_factory=sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:connection.close()

UA = {"User-Agent": "Mozilla/5.0 (alfa-hack prototype)"}
CBR = "https://www.cbr.ru"
CBR_DAILY = CBR + "/scripts/XML_daily.asp?date_req={d:%d/%m/%Y}"
CBR_DYNAMIC = CBR + "/scripts/XML_dynamic.asp?date_req1={a:%d/%m/%Y}&date_req2={b:%d/%m/%Y}&VAL_NM_RQ={id}"
CBR_METALS = CBR + "/scripts/xml_metall.asp?date_req1={a:%d/%m/%Y}&date_req2={b:%d/%m/%Y}"
CBR_KEYRATE = CBR + "/hd_base/KeyRate/?UniDbQuery.Posted=True&UniDbQuery.From={a:%d.%m.%Y}&UniDbQuery.To={b:%d.%m.%Y}"
CBR_RUONIA = CBR + "/hd_base/ruonia/dynamics/?UniDbQuery.Posted=True&UniDbQuery.From={a:%d.%m.%Y}&UniDbQuery.To={b:%d.%m.%Y}"
CBR_CPI = CBR + "/hd_base/infl/?UniDbQuery.Posted=True&UniDbQuery.From={a:%d.%m.%Y}&UniDbQuery.To={b:%d.%m.%Y}"
ISS = "https://iss.moex.com/iss"
MOEX_INDEX = ISS + "/history/engines/stock/markets/index/securities/{code}.json?from={a:%Y-%m-%d}&iss.meta=off&history.columns=TRADEDATE,CLOSE&start={start}"
MOEX_CNY = ISS + "/history/engines/currency/markets/selt/boards/CETS/securities/CNYRUB_TOM.json?from={a:%Y-%m-%d}&iss.meta=off&history.columns=TRADEDATE,CLOSE&start={start}"
MOEX_FUT = ISS + "/history/engines/futures/markets/forts/securities/{code}.json?from={a:%Y-%m-%d}&iss.meta=off&history.columns=TRADEDATE,SETTLEPRICE,CLOSE&start={start}"
MOEX_ZCYC = ISS + "/engines/stock/zcyc.json?date={d:%Y-%m-%d}&iss.meta=off&yearyields.columns=tradedate,period,value"

HISTORY_DAYS = 400           # год с запасом — для оценки волатильности
Z_EVENT = 1.5                # «заметное» движение: больше 1,5σ обычного недельного изменения
Z_STRONG = 2.5               # «сильное»: высокая важность и внеплановый пересчёт отчётов
MIN_SAMPLES = 30             # меньше — волатильность ненадёжна, используется запасной порог
REPEAT_GUARD_DAYS = 5        # не создавать событие по тому же показателю чаще

CURRENCIES = ["USD", "EUR", "CNY", "TRY", "KZT", "AED", "INR", "BYN", "HKD", "DKK"]
COUNTRY_CURRENCY = {"КИТАЙ": "CNY", "ГОНКОНГ": "HKD", "ТУРЦИЯ": "TRY", "КАЗАХСТАН": "KZT", "ОАЭ": "AED",
                    "ИНДИЯ": "INR", "БЕЛАРУСЬ": "BYN", "БЕЛОРУССИЯ": "BYN", "ДАНИЯ": "DKK"}
METALS = {"1": "gold", "2": "silver", "3": "platinum", "4": "palladium"}
MONTH_CODES = "FGHJKMNQUVXZ"


def _ind(name, unit, source, kind, floor, fallback, mechanism, event="volatility"):
    """kind: price — изменения в %, rate — в п.п.; floor — минимальное значимое движение;
    fallback — порог, пока истории мало для оценки волатильности."""
    return {"name": name, "unit": unit, "source": source, "kind": kind, "floor": floor,
            "fallback": fallback, "mechanism": mechanism, "event": event}


INDICATORS = {
    "key_rate": _ind("Ключевая ставка Банка России", "% годовых", "Банк России", "rate", 0, 0, "rate", "any_change"),
    "ruonia": _ind("Ставка RUONIA", "% годовых", "Банк России", "rate", 0.25, 0.5, "money"),
    "cpi_yoy": _ind("Инфляция (ИПЦ), г/г", "%", "Банк России", "rate", 0.3, 0.5, "cpi", "monthly"),
    "ofz_1y": _ind("Доходность ОФЗ на 1 год (кривая МосБиржи)", "% годовых", "Московская биржа", "rate", 0.15, 0.4, "ofz"),
    "ofz_5y": _ind("Доходность ОФЗ на 5 лет (кривая МосБиржи)", "% годовых", "Московская биржа", "rate", 0.15, 0.4, "ofz"),
    "ofz_10y": _ind("Доходность ОФЗ на 10 лет (кривая МосБиржи)", "% годовых", "Московская биржа", "rate", 0.15, 0.4, "ofz"),
    "cny_moex": _ind("Биржевой курс юаня CNY/RUB (МосБиржа, TOM)", "руб.", "Московская биржа", "price", 1.0, 3.0, "fx"),
    "imoex": _ind("Индекс МосБиржи (IMOEX)", "пунктов", "Московская биржа", "price", 2.0, 5.0, "imoex"),
    "rgbi": _ind("Индекс гособлигаций RGBI", "пунктов", "Московская биржа", "price", 0.5, 2.0, "ofz"),
    "brent": _ind("Нефть Brent (ближайший фьючерс МосБиржи)", "долл./барр.", "Московская биржа", "price", 3.0, 7.0, "brent"),
    "gold": _ind("Учётная цена золота Банка России", "руб./г", "Банк России", "price", 2.0, 5.0, "metal"),
    "silver": _ind("Учётная цена серебра Банка России", "руб./г", "Банк России", "price", 2.0, 6.0, "metal"),
    "platinum": _ind("Учётная цена платины Банка России", "руб./г", "Банк России", "price", 2.0, 6.0, "metal"),
    "palladium": _ind("Учётная цена палладия Банка России", "руб./г", "Банк России", "price", 2.0, 6.0, "metal"),
    **{f"fx_{c}": _ind(f"Официальный курс {c}/RUB (Банк России)", "руб.", "Банк России", "price", 1.0, 3.0, "fx")
       for c in CURRENCIES},
}

# Однонаправленные движения в группе — одно событие («рубль укрепился к основным валютам»), а не N почти одинаковых.
GROUPS = {
    "fx": {"members": ["cny_moex"] + [f"fx_{c}" for c in CURRENCIES], "mechanism": "fx"},
    "metals": {"members": ["gold", "silver", "platinum", "palladium"], "mechanism": "metal"},
    "ofz": {"members": ["ofz_1y", "ofz_5y", "ofz_10y"], "mechanism": "ofz"},
}

MECHANISMS = {
    "rate": ("Изменение ключевой ставки передаётся в стоимость кредитов, лизинга и доходность депозитов с лагом.",
             "Как изменение ключевой ставки влияет на стоимость заёмного финансирования и доходность размещения средств компаний?",
             "Российские компании с кредитами, лизингом или депозитами; эффект зависит от доли плавающих ставок и сроков."),
    "money": ("Ставки денежного рынка задают стоимость краткосрочного фондирования банков и плавающих кредитов.",
              "Как изменение ставок денежного рынка (RUONIA) влияет на стоимость краткосрочных и плавающих кредитов компаний?",
              "Заёмщики с плавающими ставками, привязанными к RUONIA или ключевой ставке; депозиты и остатки на счетах."),
    "ofz": ("Доходности гособлигаций задают ориентир стоимости долгового финансирования и ожидания по ставкам.",
            "Как изменение доходностей ОФЗ влияет на стоимость облигационного и банковского финансирования компаний?",
            "Эмитенты облигаций и заёмщики; эффект проявляется с лагом через рыночные ставки."),
    "cpi": ("Изменение инфляции влияет на издержки, цены реализации, зарплатные ожидания и решения по ставке.",
            "Как изменение инфляции влияет на издержки, цены и маржу компаний и на ожидания по ключевой ставке?",
            "Российские компании; величина эффекта зависит от структуры затрат и возможности переносить их в цены."),
    "fx": ("Изменение курса рубля меняет рублёвую стоимость импорта и выручку от экспорта в валюте.",
           "Как изменение курса рубля к валюте влияет на стоимость импортных закупок и экспортную выручку компаний?",
           "Компании с внешнеторговыми контрактами в этой валюте; направление эффекта зависит от импорта или экспорта."),
    "imoex": ("Движение фондового рынка отражает ожидания инвесторов и влияет на стоимость акционерного капитала.",
              "Как изменение российского фондового рынка связано с условиями привлечения капитала компаниями?",
              "Публичные компании и эмитенты; для непубличных компаний — косвенный индикатор настроений."),
    "brent": ("Цена нефти влияет на стоимость топлива, выручку нефтяного сектора, бюджет и курс рубля.",
              "Как изменение мировых цен на нефть влияет на топливные издержки, нефтегазовую выручку и курс рубля?",
              "Компании топливного рынка, транспорта и логистики, энергоёмкие производства, экспортёры нефти."),
    "metal": ("Изменение цен драгоценных металлов влияет на выручку добытчиков и стоимость сырья для переработки.",
              "Как изменение цен на драгоценные металлы влияет на компании добычи, переработки и ювелирной отрасли?",
              "Добыча и переработка драгоценных металлов, ювелирное производство, промышленные потребители металлов."),
}


# ---------- сбор ----------

def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        return r.read()


def _num(text: str) -> float:
    return float(str(text).replace("\xa0", "").replace(" ", "").replace(",", "."))


class _Table(HTMLParser):
    """Строки HTML-таблиц как списки текстов ячеек (страницы базы данных Банка России)."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def _cbr_table(url: str, date_format: str) -> list[tuple[str, list[str]]]:
    parser = _Table()
    parser.feed(_get(url).decode("utf-8", errors="replace"))
    rows = []
    for r in parser.rows:
        try:
            d = datetime.strptime(r[0], date_format).date()
        except (ValueError, IndexError):
            continue
        rows.append((d.isoformat(), r[1:]))
    return rows


def _moex_pages(template: str, **kwargs) -> list[list]:
    """История ISS отдаётся страницами по 100 строк."""
    data, start = [], 0
    while True:
        page = json.loads(_get(template.format(start=start, **kwargs)))["history"]["data"]
        data += page
        if len(page) < 100:
            return data
        start += len(page)


def fetch_currencies(start: date, end: date) -> list[tuple]:
    root = ET.fromstring(_get(CBR_DAILY.format(d=end)))
    ids = {v.findtext("CharCode"): v.get("ID") for v in root.iter("Valute")}
    rows = []
    for code in CURRENCIES:
        if code in ids:
            for rec in ET.fromstring(_get(CBR_DYNAMIC.format(a=start, b=end, id=ids[code]))).iter("Record"):
                rows.append((f"fx_{code}", datetime.strptime(rec.get("Date"), "%d.%m.%Y").date().isoformat(),
                             _num(rec.findtext("Value")) / float(rec.findtext("Nominal") or 1)))
    return rows


def fetch_metals(start: date, end: date) -> list[tuple]:
    return [(METALS[r.get("Code")], datetime.strptime(r.get("Date"), "%d.%m.%Y").date().isoformat(), _num(r.findtext("Buy")))
            for r in ET.fromstring(_get(CBR_METALS.format(a=start, b=end))).iter("Record") if r.get("Code") in METALS]


def fetch_key_rate(start: date, end: date) -> list[tuple]:
    return [("key_rate", d, _num(cells[0])) for d, cells in _cbr_table(CBR_KEYRATE.format(a=start, b=end), "%d.%m.%Y")]


def fetch_ruonia(start: date, end: date) -> list[tuple]:
    return [("ruonia", d, _num(cells[0])) for d, cells in _cbr_table(CBR_RUONIA.format(a=start, b=end), "%d.%m.%Y")]


def fetch_cpi(start: date, end: date) -> list[tuple]:
    # месячное значение г/г; дата — первое число месяца, к которому относится значение
    return [("cpi_yoy", d, _num(cells[1])) for d, cells in _cbr_table(CBR_CPI.format(a=start, b=end), "%m.%Y")
            if len(cells) > 1 and cells[1]]


def fetch_moex_index(code: str, start: date) -> list[tuple]:
    return [(code, d, float(v)) for d, v in _moex_pages(MOEX_INDEX, code=code.upper(), a=start) if v is not None]


def fetch_cny_moex(start: date) -> list[tuple]:
    return [("cny_moex", d, float(v)) for d, v in _moex_pages(MOEX_CNY, a=start) if v is not None]


def fetch_brent(start: date, end: date) -> list[tuple]:
    """Непрерывный ряд ближайшего фьючерса: на каждую дату берётся контракт с самым ранним исполнением."""
    contracts, d = [], date(start.year, start.month, 1)
    while d <= end + timedelta(days=62):
        contracts.append(f"BR{MONTH_CODES[d.month - 1]}{d.year % 10}")
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    series: dict[str, dict[str, float]] = {}
    for code in contracts:
        try:
            rows = _moex_pages(MOEX_FUT, code=code, a=start)
        except Exception:  # несуществующий или ещё не торгуемый контракт
            continue
        prices = {r[0]: float(r[1] or r[2]) for r in rows if (r[1] or r[2])}
        if prices:
            series[code] = prices
    last_day = {code: max(p) for code, p in series.items()}
    result = {}
    for code in sorted(series, key=lambda c: last_day[c]):
        for day, price in series[code].items():
            result.setdefault(day, price)   # первым ставится контракт с более ранним исполнением
    return [("brent", d, v) for d, v in sorted(result.items())]


def fetch_ofz(dates: list[date]) -> list[tuple]:
    rows = []
    for d in dates:
        try:
            data = json.loads(_get(MOEX_ZCYC.format(d=d))).get("yearyields", {}).get("data", [])
        except Exception:
            continue
        for tradedate, period, value in data:
            if period in (1, 5, 10) and value is not None:
                rows.append((f"ofz_{int(period)}y", tradedate, float(value)))
    return rows


def _date_ranges() -> dict[str, tuple[str, str]]:
    with _connect() as con:
        return {r["code"]: (r["a"], r["b"]) for r in con.execute(
            "select code, min(date) a, max(date) b from indicators group by code")}


def collect(days: int = HISTORY_DAYS, log=print) -> int:
    """Догрузить историю показателей: при первом запуске — за год, дальше — последние дни."""
    end = date.today()
    full_start = end - timedelta(days=days)
    ranges = _date_ranges()

    def start_for(*codes):
        """Год истории, если её нет или она короче; иначе — последние 10 дней."""
        for c in codes:
            if c not in ranges or date.fromisoformat(ranges[c][0]) > full_start + timedelta(days=30):
                return full_start
        return max(full_start, date.fromisoformat(min(ranges[c][1] for c in codes)) - timedelta(days=10))

    # кривая ОФЗ доступна только на дату: для истории — по одной точке в неделю (рабочие дни), дальше — ежедневно
    last_workday = end - timedelta(days=max(0, end.weekday() - 4))
    ofz_known = {r["date"] for r in _rows("ofz_5y")}
    weekly = [last_workday - timedelta(days=7 * i) for i in range(days // 7, 0, -1)]
    recent = [end - timedelta(days=i) for i in range(10) if (end - timedelta(days=i)).weekday() < 5]
    ofz_dates = sorted({d for d in weekly + recent if d.isoformat() not in ofz_known})
    jobs = [
        ("курсы ЦБ", lambda: fetch_currencies(start_for(*[f"fx_{c}" for c in CURRENCIES]), end)),
        ("металлы ЦБ", lambda: fetch_metals(start_for(*METALS.values()), end)),
        ("ключевая ставка", lambda: fetch_key_rate(start_for("key_rate"), end)),
        ("RUONIA", lambda: fetch_ruonia(start_for("ruonia"), end)),
        ("инфляция", lambda: fetch_cpi(end - timedelta(days=days + 60), end)),
        ("IMOEX", lambda: fetch_moex_index("imoex", start_for("imoex"))),
        ("RGBI", lambda: fetch_moex_index("rgbi", start_for("rgbi"))),
        ("юань МосБиржи", lambda: fetch_cny_moex(start_for("cny_moex"))),
        ("Brent", lambda: fetch_brent(start_for("brent"), end)),
        ("ОФЗ", lambda: fetch_ofz(ofz_dates)),
    ]
    rows, errors = [], []
    for name, job in jobs:
        try:
            got = job()
            rows += got
            if not got:
                errors.append(f"{name}: нет данных")
        except Exception as error:  # недоступность одного источника не должна ломать остальные
            errors.append(f"{name}: {type(error).__name__}: {str(error)[:100]}")
    ts = store.now()
    with _connect() as con:
        con.executemany(
            "insert into indicators (code, date, value, source, fetched_at) values (?,?,?,?,?)"
            " on conflict(code, date) do update set value=excluded.value, fetched_at=excluded.fetched_at",
            [(code, d, v, INDICATORS[code]["source"], ts) for code, d, v in rows])
    log(f"Показатели: сохранено {len(rows)} значений" + (f"; проблемы: {errors}" if errors else ""))
    return len(rows)


# ---------- волатильность и события ----------

def _rows(code: str, days: int = HISTORY_DAYS) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    with _connect() as con:
        return [dict(r) for r in con.execute(
            "select date, value from indicators where code = ? and date >= ? order by date", (code, since))]


def history(code: str, days: int = 60) -> list[tuple[str, float]]:
    return [(r["date"], r["value"]) for r in _rows(code, days)]


def _before(series: list[tuple[str, float]], day: str, days: int) -> tuple[str, float] | None:
    target = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    earlier = [p for p in series if p[0] <= target]
    return earlier[-1] if earlier else None


def _change(code: str, old: float, new: float) -> float:
    return (new - old) if INDICATORS[code]["kind"] == "rate" else (new / old - 1) * 100 if old else 0.0


def _robust_sigma(values: list[float]) -> float | None:
    """Устойчивая оценка σ по медиане абсолютных отклонений (1.4826·MAD).

    Обычное стандартное отклонение раздувают редкие выбросы — например, скачки при смене
    месячного контракта Brent; на MAD они почти не влияют.
    """
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return 1.4826 * mad or statistics.pstdev(values) or None


def weekly_stats(code: str) -> dict | None:
    """Текущее недельное изменение, обычное недельное колебание (σ) и z-оценка."""
    series = history(code, HISTORY_DAYS)
    if len(series) < 2:
        return None
    period = 31 if INDICATORS[code]["event"] == "monthly" else 7
    last_day, last = series[-1]
    before = _before(series, last_day, period)
    if not before:
        return None
    current = _change(code, before[1], last)
    cutoff = (date.fromisoformat(last_day) - timedelta(days=period)).isoformat()
    past = []
    for day, value in series:
        if day > cutoff:
            break
        prev = _before(series, day, period)
        if prev and prev[0] < day:
            past.append(_change(code, prev[1], value))
    sigma = _robust_sigma(past) if len(past) >= MIN_SAMPLES else None
    return {"date": last_day, "value": last, "since": before[0], "base": before[1], "change": round(current, 4),
            "sigma": round(sigma, 4) if sigma else None, "z": round(current / sigma, 2) if sigma else None,
            "samples": len(past), "unit": "п.п." if INDICATORS[code]["kind"] == "rate" else "%"}


def is_event(code: str, stats: dict) -> bool:
    cfg = INDICATORS[code]
    move = abs(stats["change"])
    if cfg["event"] == "any_change":
        return move > 0
    if stats["z"] is None:                       # истории мало — запасной фиксированный порог
        return move >= cfg["fallback"]
    return abs(stats["z"]) >= Z_EVENT and move >= cfg["floor"]


def tier(code: str, stats: dict) -> str:
    """strong — высокая важность и пересчёт отчётов; notable — в ленту и в плановый отчёт."""
    if code == "key_rate" or (stats["z"] is not None and abs(stats["z"]) >= Z_STRONG):
        return "strong"
    if stats["z"] is None and abs(stats["change"]) >= 2 * INDICATORS[code]["fallback"]:
        return "strong"
    return "notable"


def _fmt(x: float, digits: int = 2) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def _val(x: float) -> str:
    """Значение показателя: 4 значащие цифры, десятичная запятая."""
    return f"{x:.4g}".replace(".", ",")


def _with_unit(x: float, unit: str) -> str:
    return f"{_val(x)} {unit}"


def _event_doc(code: str, s: dict) -> dict:
    cfg = INDICATORS[code]
    rate = cfg["kind"] == "rate"
    direction = "рост" if s["change"] > 0 else "снижение"   # без согласования по роду: «нефть», «курс», «доходность»
    move = f"{_fmt(abs(s['change']))}{'%' if s['unit'] == '%' else ' п.п.'}"
    if code == "key_rate":
        thesis = f"Банк России изменил ключевую ставку с {_fmt(s['base'])} до {_fmt(s['value'])}% годовых (действует с {s['date']})."
        title = f"Ключевая ставка изменена до {_fmt(s['value'])}%"
    else:
        what = cfg["name"]
        period = "за месяц" if cfg["event"] == "monthly" else f"за неделю ({s['since']} — {s['date']})"
        thesis = f"{what}: {direction} на {move} {period}, с {_val(s['base'])} до {_with_unit(s['value'], cfg['unit'])}".rstrip(".") + "."
        title = f"{what}: {direction} на {move}"
    sigma_unit = "%" if s["unit"] == "%" else " п.п."
    volatility = (f" Движение в {_fmt(abs(s['z']), 1)} раза больше обычного недельного колебания "
                  f"(σ = {_fmt(s['sigma'])}{sigma_unit} по {s['samples']} наблюдениям за год)." if s.get("z") else "")
    description, question, conditions = MECHANISMS[cfg["mechanism"]]
    context = (f"Источник данных: {cfg['source']}. Значение на {s['since']}: {_with_unit(s['base'], cfg['unit'])}; "
               f"на {s['date']}: {_with_unit(s['value'], cfg['unit'])}".rstrip(".") + f".{volatility} Причины изменения в данных не указаны.")
    fragment = {
        "id": "F001", "title": cfg["name"], "thesis": thesis, "context": context,
        "company_embedding_text": f"{thesis}\n\n{context}",
        "mechanism": {"description": description, "status": "hypothesis", "is_established_cause": False,
                      "source_attributions": [], "boundaries": ["Причина движения показателя данными не установлена."],
                      "context": {"conditions": conditions}},
        "analytics_search_question": question,
        "analytics_embedding_text": f"{question}\nУсловия и границы применимости: {conditions}",
        "source_block_ids": [], "source_quotes": [], "attributions": [{"author": cfg["source"], "statement": thesis}],
        "sectors": [], "countries": ["Россия"], "regions": [], "organizations": [cfg["source"]], "okved_codes": [],
        "event_date_or_period": f"{s['since']} — {s['date']}", "limitations": [],
    }
    return {
        "schema_version": "news-two-views-1.0",
        "document_id": f"indicator:{code}:{s['date']}",
        "document_title": title,
        "document_date": s["date"],
        "source": {"url": None, "publisher": cfg["source"]},
        "preparation": {"source_access": "full_text", "prepared_by": "indicator_template"},
        "document_limitations": ["Событие сформировано автоматически по рыночным данным, а не по публикации СМИ."],
        "coverage": {"processed_block_ids": [], "excluded_blocks": [], "unprocessed_block_ids": []},
        "indicator": {"code": code, **s},
        "fragments": [fragment],
    }


def _recent_event(code: str, day: str) -> bool:
    since = (date.fromisoformat(day) - timedelta(days=REPEAT_GUARD_DAYS)).isoformat()
    with _connect() as con:
        return con.execute("select 1 from news where kind = 'indicator' and id like ? and published_utc >= ?",
                           (f"indicator:{code}:%", since)).fetchone() is not None


SHORT = {"cny_moex": "CNY (биржевой)", "gold": "золото", "silver": "серебро", "platinum": "платина",
         "palladium": "палладий", "ofz_1y": "1 год", "ofz_5y": "5 лет", "ofz_10y": "10 лет"}


def _short(code: str) -> str:
    return SHORT.get(code) or (code[3:] if code.startswith("fx_") else INDICATORS[code]["name"])


def _group_doc(group: str, members: list[tuple[str, dict]]) -> dict:
    """Одно событие на группу однонаправленных движений; в тексте — каждое движение с z-оценкой."""
    up = members[0][1]["change"] > 0
    date_ = max(st["date"] for _, st in members)
    since = min(st["since"] for _, st in members)
    lines = [f"{_short(c)} {'+' if st['change'] > 0 else '−'}{_fmt(abs(st['change']))}"
             f"{'%' if st['unit'] == '%' else ' п.п.'} ({_fmt(abs(st['z'] or 0), 1)}σ)"
             for c, st in members]
    if group == "fx":
        head = f"Рубль {'ослаб' if up else 'укрепился'} к {len(members)} валютам за неделю"
        thesis = f"{head} ({since} — {date_}), курсы к рублю: " + ", ".join(lines) + "."
    elif group == "metals":
        head = f"Драгоценные металлы {'подорожали' if up else 'подешевели'} за неделю"
        thesis = f"{head} ({since} — {date_}), учётные цены Банка России: " + ", ".join(lines) + "."
    else:
        head = f"Доходности ОФЗ {'выросли' if up else 'снизились'} по {len(members)} срокам за неделю"
        thesis = f"{head} ({since} — {date_}), сроки: " + ", ".join(lines) + "."
    zmax = max(abs(st["z"] or 0) for _, st in members)
    level = "strong" if zmax >= Z_STRONG else "notable"
    description, question, conditions = MECHANISMS[GROUPS[group]["mechanism"]]
    context = (f"Источники данных: {', '.join(sorted({INDICATORS[c]['source'] for c, _ in members}))}. "
               "В скобках — во сколько раз движение больше обычного недельного колебания (σ) за последний год. "
               "Однонаправленные движения объединены в одно событие. Причины изменения в данных не указаны.")
    return {
        "schema_version": "news-two-views-1.0",
        "document_id": f"indicator:group_{group}:{date_}",
        "document_title": head,
        "document_date": date_,
        "source": {"url": None, "publisher": "Банк России, Московская биржа"},
        "preparation": {"source_access": "full_text", "prepared_by": "indicator_template"},
        "document_limitations": ["Событие сформировано автоматически по рыночным данным, а не по публикации СМИ."],
        "coverage": {"processed_block_ids": [], "excluded_blocks": [], "unprocessed_block_ids": []},
        "indicator": {"code": f"group_{group}", "members": [{"code": c, **st} for c, st in members],
                      "z": zmax, "change": members[0][1]["change"], "unit": members[0][1]["unit"], "tier": level},
        "fragments": [{
            "id": "F001", "title": head, "thesis": thesis, "context": context,
            "company_embedding_text": f"{thesis}\n\n{context}",
            "mechanism": {"description": description, "status": "hypothesis", "is_established_cause": False,
                          "source_attributions": [], "boundaries": ["Причина движения данными не установлена."],
                          "context": {"conditions": conditions}},
            "analytics_search_question": question,
            "analytics_embedding_text": f"{question}\nУсловия и границы применимости: {conditions}",
            "source_block_ids": [], "source_quotes": [], "attributions": [], "sectors": [], "countries": ["Россия"],
            "regions": [], "organizations": [], "okved_codes": [], "event_date_or_period": f"{since} — {date_}", "limitations": [],
        }],
    }


def find_events() -> list[dict]:
    """Документы-события на текущих данных (без сохранения)."""
    passed = {}
    for code in INDICATORS:
        s = weekly_stats(code)
        if s and is_event(code, s):
            passed[code] = s
    docs, grouped = [], set()
    for group, cfg in GROUPS.items():
        members = [(c, passed[c]) for c in cfg["members"] if c in passed]
        for sign in (1, -1):
            same = [(c, st) for c, st in members if (st["change"] > 0) == (sign > 0)]
            if len(same) >= 2:
                docs.append(_group_doc(group, sorted(same, key=lambda x: -abs(x[1]["z"] or 0))))
                grouped |= {c for c, _ in same}
    for code, st in passed.items():
        if code not in grouped:
            doc = _event_doc(code, st)
            doc["indicator"]["tier"] = tier(code, st)
            docs.append(doc)
    return docs


def detect_events(log=print) -> list[dict]:
    """Резкие движения → документы-события (сохраняются как новости kind = indicator)."""
    from .news import save_news

    saved = []
    for doc in find_events():
        code = doc["indicator"]["code"]
        if _recent_event(code, doc["document_date"]):
            continue
        level = "high" if doc["indicator"]["tier"] == "strong" else "normal"
        save_news(doc, "indicator", f"{doc['document_date']}T00:00:00+00:00", level)
        saved.append(doc)
    log(f"Событий по показателям: {len(saved)}" + (
        f" ({'; '.join(d['document_title'] + ' [' + d['indicator']['tier'] + ']' for d in saved)})" if saved else ""))
    return saved


# ---------- для пакета ----------

def snapshot(codes: list[str]) -> list[dict]:
    """Последние значения, изменения за неделю и месяц, обычная волатильность — блок для пакета."""
    result = []
    for code in codes:
        cfg = INDICATORS[code]
        series = history(code, 120 if cfg["event"] == "monthly" else 60)
        if not series:
            continue
        last_date, last = series[-1]
        item = {"code": code, "name": cfg["name"], "unit": cfg["unit"], "source": cfg["source"],
                "date": last_date, "value": round(last, 4)}
        periods = (("change_1m", 30),) if cfg["event"] == "monthly" else (("change_1w", 7), ("change_1m", 30))
        for label, days in periods:
            before = _before(series, last_date, days)
            if before:
                change = _change(code, before[1], last)
                item[label] = {"since": before[0], "value": round(before[1], 4),
                               "pct" if cfg["kind"] == "price" else "pp": round(change, 2)}
        stats = weekly_stats(code)
        if stats and stats["sigma"] and cfg["event"] == "volatility":
            item["typical_weekly_move"] = {"sigma": stats["sigma"], "unit": stats["unit"], "z_1w": stats["z"]}
        result.append(item)
    return result


def codes_for_company(facts: dict, aspects: list[dict]) -> list[str]:
    """Показатели, к которым компания чувствительна по своим фактам."""
    codes = ["key_rate", "cpi_yoy", "fx_USD", "fx_EUR", "cny_moex", "imoex"]
    flags = facts.get("banking_product_flags") or {}
    products = {p.get("product") for p in facts.get("banking_products") or [] if isinstance(p, dict)}  # группа
    if flags.get("loans") or flags.get("leasing") or flags.get("deposits") or products & {"кредиты", "лизинг", "депозиты"}:
        codes += ["ruonia", "ofz_1y", "ofz_5y", "rgbi"]
    for c in (facts.get("foreign_trade") or {}).get("countries") or []:
        name = c["country"] if isinstance(c, dict) else c
        code = f"fx_{COUNTRY_CURRENCY[name]}" if name in COUNTRY_CURRENCY else None
        if code and code not in codes and code != "fx_CNY":   # юань уже есть — биржевой курс
            codes.append(code)
    okved = str((facts.get("industry") or {}).get("main_okved_code") or "")
    divisions = {d.get("division") for d in facts.get("industry_mix") or [] if isinstance(d, dict)}  # группа
    oil = ("06", "09", "19", "35", "46.71", "47.3", "49", "50", "51", "52")
    if okved.startswith(oil) or divisions & {"06", "09", "19", "35", "49", "50", "51"}:
        codes.append("brent")
    if okved.startswith(("07", "24.41", "32.12", "46.72", "46.48")) or divisions & {"07"}:
        codes += ["gold", "silver", "platinum", "palladium"]
    return list(dict.fromkeys(codes))
