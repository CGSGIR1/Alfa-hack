"""
Очистка текстов новостей — общий модуль для парсера (parser.py) и миграции (migrate.py):
новые и уже собранные записи готовятся одними и теми же функциями.

Здесь только работа с текстом: ни базы данных, ни сети.
"""

import hashlib
import html
import re
from collections import defaultdict
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

# Сколько символов максимум идёт в текст для эмбеддинга (embed_text).
EMBED_MAX_CHARS = 1500
# Абзац, который дословно повторяется в стольких разных новостях одного издания,
# считается шаблонной справкой и вырезается из body_clean.
BOILERPLATE_MIN_NEWS = 2
# Лид — первый абзац не короче этого числа символов, похожий на законченное предложение.
LEAD_MIN_LEN = 60
# Сколько первых строк текста проверяем на повтор заголовка и подзаголовка.
HEADER_MAX_LINES = 3
# Строка короче этого не считается «куском заголовка» (см. strip_header).
HEADER_MIN_PART = 15
# Шаблонными считаются только строки короче этого: длинный абзац — уже текст статьи.
TEMPLATE_MAX_LEN = 200
# Абзац, обрезанный под лимит embed_text, короче этого не добавляем.
MIN_CUT_LEN = 100


# ---------- HTML и служебные строки (при скачивании статьи) ----------

def clean_html(raw):
    """Убирает HTML-теги и превращает &nbsp;, &laquo; и т.п. в обычные символы."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# Строки, которые сайты вставляют в статью, но к её тексту они не относятся:
# копирайты и подписи к фото, ссылки «читайте также», соцсети, медиабанк.
JUNK_LINE_PATTERNS = [
    r"©",
    r"перейти в медиабанк",
    r"^читать .{0,40} в\b",
    r"^читайте (также|нас)",
    r"^(фото|фотография|иллюстрация|источник фото)\s*:",
    r"^подпишитесь",
    r"^(реклама|erid)\b",
    r"^(telegram|телеграм|вконтакте|vk|дзен|одноклассники|rutube|max)$",
    r"t\.me/",
]
JUNK_RE = re.compile("|".join(JUNK_LINE_PATTERNS), re.IGNORECASE)
# Проверяем только короткие строки: настоящий абзац статьи длинный,
# и его нельзя выкинуть из-за случайного слова вроде «©» в цитате.
JUNK_MAX_LEN = 150

# Подводка агентств в начале новости: «МОСКВА, 19 окт — ПРАЙМ.»
DATELINE_RE = re.compile(r"^[А-ЯЁ][А-ЯЁ\- ]+,\s*\d{1,2}\s+\w+\.?\s*[—–-]+\s*[А-ЯЁA-Z \"«»]+\.\s*")


def clean_article(text):
    """Убирает из статьи служебные строки (копирайты фото, ссылки на соцсети и т.п.)."""
    lines = [ln.strip() for ln in text.split("\n")]
    kept = [ln for ln in lines if ln and not (len(ln) < JUNK_MAX_LEN and JUNK_RE.search(ln))]
    return "\n".join(kept)


def make_lead(text, max_len=400):
    """
    Анонс из статьи, если в RSS его нет: первый настоящий абзац —
    достаточно длинный и похожий на предложение, без подводки «МОСКВА, … — ПРАЙМ.».
    """
    for line in text.split("\n"):
        line = DATELINE_RE.sub("", line.strip())
        if len(line) >= 60 and re.search(r"[.!?»…]$", line):
            break
    else:
        line = DATELINE_RE.sub("", text.strip().split("\n")[0])
    return line if len(line) <= max_len else line[:max_len].rsplit(" ", 1)[0] + "…"


# ---------- дата, издание, раздел сайта ----------

def to_utc_iso(published):
    """
    Дата из RSS в ISO 8601 по UTC: «Fri, 16 Oct 2026 00:32:41 +0300» →
    «2026-10-15T21:32:41+00:00». Такие строки сортируются по времени правильно.
    Если дату разобрать не удалось — None.
    """
    try:
        dt = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # пояс «-0000» в RSS означает «неизвестен» — считаем UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def split_source(source):
    """«vedomosti_finance» → («vedomosti», «finance»): издание и рубрика RSS-ленты."""
    outlet, _, rubric = (source or "").partition("_")
    return outlet or None, rubric or None


def url_section_from_url(url):
    """Раздел сайта из адреса: vedomosti.ru/finance/news/… → «finance».
    У Коммерсанта адреса вида /doc/123 раздела не содержат — тогда None."""
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    if not parts or parts[0] == "doc":
        return None
    return parts[0].lower()


# ---------- сравнение строк ----------

QUOTES_RE = re.compile(r"[«»“”„‟\"'`’‘‚]")
DASHES_RE = re.compile(r"[‐‑‒–—―]")


def norm_space(s):
    """Неразрывные пробелы → обычные, лишние пробелы схлопнуты."""
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm_compare(s):
    """Строка для сравнения без мелочей: нижний регистр, ё → е, без кавычек,
    все тире одинаковые, без точки в конце."""
    s = QUOTES_RE.sub("", norm_space(s).lower().replace("ё", "е"))
    s = DASHES_RE.sub("-", s)
    return re.sub(r"\s+", " ", s).strip(" .")


def paragraphs(text):
    """Текст → список абзацев (непустые строки)."""
    return [p for p in map(norm_space, (text or "").split("\n")) if p]


# ---------- заголовок, подзаголовок, шапка текста ----------

def split_title(title_raw):
    """«Заголовок // Подзаголовок» (так в RSS пишет Коммерсант) → (заголовок, подзаголовок).
    Если разделителя нет — (заголовок, None)."""
    title_raw = norm_space(title_raw)
    parts = re.split(r"\s+//\s+", title_raw, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return title_raw, None


def strip_header(paras, headers):
    """
    Убирает первые строки текста, которые повторяют заголовок или подзаголовок
    (сайты ставят их в начало статьи). Строка — повтор, если после norm_compare она
    совпадает с одним из headers или целиком входит в него: в интервью Ведомостей
    первая строка — заголовок без имени («Сергей Черногаев: «…»» → «…»).
    """
    heads = [h for h in map(norm_compare, headers) if h]
    i = 0
    while i < min(HEADER_MAX_LINES, len(paras)):
        line = norm_compare(paras[i])
        if line in heads or (len(line) >= HEADER_MIN_PART and any(line in h for h in heads)):
            i += 1
        else:
            break
    return paras[i:]


def guess_deck(summary, text, title_raw):
    """
    Врезка Ведомостей — короткий подзаголовок статьи. В RSS она приходит как анонс
    (description), а в тексте стоит сразу после заголовка. От первого абзаца её отличает
    то, что в конце нет точки (! ? …). Возвращает врезку или None.
    """
    summary = norm_space(summary)
    if not summary or re.search(r"[.!?…]$", summary):
        return None
    rest = strip_header(paragraphs(text), [title_raw])
    if rest and norm_compare(rest[0]) == norm_compare(summary):
        return summary
    return None


# ---------- пейволл и шаблонные строки ----------

# Пейволл Ведомостей: обрезанная статья кончается строкой «Вы видите 10% этого материала».
PAYWALL_RE = re.compile(r"Вы видите\s*(\d{1,3})\s*%\s*этого материала", re.IGNORECASE)


def detect_paywall(text):
    """Если статья обрезана пейволлом, возвращает, сколько процентов видно (10), иначе None."""
    m = PAYWALL_RE.search(text or "")
    return int(m.group(1)) if m else None


# Шаблонные строки, которые издания добавляют к тексту статьи. Как и JUNK_LINE_PATTERNS,
# проверяются только короткие строки целиком, поэтому «передает ТАСС» или
# «(цитата по «РИА Новости»)» внутри предложения остаются — это атрибуция.
TEMPLATE_LINE_PATTERNS = [
    r"^Газета «Коммерсантъ» №",              # реквизиты печатного номера Коммерсанта…
    r"^от \d{2}\.\d{2}\.\d{4}, стр\. \d+$",  # …и его дата и полоса
    r"^Подробнее\s*[—–-]",                   # «Подробнее — в материале «Ъ» «…»»
    r"[—–-]\s*в материал\w*\s*«Ъ»",          # «О ситуации на рынке труда — в материале «Ъ» «…»»
    r"доступны на специальной странице",     # «Актуальные курсы … доступны на специальной странице»
    r"^подпи(сывай|ши)",                     # «Подписывайтесь…», «Подпишитесь…»
    r"t\.me/",                               # ссылки на Telegram
    r"^читайте (также|нас)",
]
TEMPLATE_RE = re.compile("|".join(TEMPLATE_LINE_PATTERNS), re.IGNORECASE)


def is_template_line(line):
    """True для служебной строки: шаблон издания или мусор вроде копирайта фото."""
    return ((len(line) < TEMPLATE_MAX_LEN and TEMPLATE_RE.search(line) is not None)
            or (len(line) < JUNK_MAX_LEN and JUNK_RE.search(line) is not None))


# ---------- текст статьи ----------

def body_paragraphs(text, title, subtitle=None, title_raw=None):
    """Абзацы текста без повтора заголовка в начале и без строки пейволла."""
    paras = strip_header(paragraphs(text), [title_raw, title, subtitle])
    return [p for p in paras if not (len(p) < TEMPLATE_MAX_LEN and PAYWALL_RE.search(p))]


def find_boilerplate(articles, min_news=BOILERPLATE_MIN_NEWS):
    """
    Шаблонные абзацы — те, что дословно повторяются в min_news и более разных
    новостях одного издания: справки, которые Ведомости приклеивают в конец заметок,
    реквизиты печатного номера Коммерсанта и т.п.

    articles — словари с ключами id, outlet, text, title, subtitle, title_raw.
    Возвращает {издание: {абзац: в скольких новостях он встретился}}.
    """
    news_with = defaultdict(set)
    for a in articles:
        for p in set(body_paragraphs(a["text"], a["title"], a.get("subtitle"), a.get("title_raw"))):
            news_with[(a["outlet"], p)].add(a["id"])
    found = defaultdict(dict)
    for (outlet, p), ids in news_with.items():
        if len(ids) >= min_news:
            found[outlet][p] = len(ids)
    return dict(found)


def clean_body(text, title, subtitle=None, title_raw=None, boilerplate=()):
    """
    Текст статьи для разметки (body_clean):
      1) без повтора заголовка и подзаголовка в начале;
      2) без строки пейволла «Вы видите N% этого материала»;
      3) без служебных строк (реквизиты номера, «Подробнее — …», «Подписывайтесь…»);
      4) без шаблонных абзацев издания (boilerplate, см. find_boilerplate). Лид не
         вырезается никогда: если его дословно процитировали в другой новости,
         эта новость не должна остаться без главного абзаца.
    """
    paras = [p for p in body_paragraphs(text, title, subtitle, title_raw) if not is_template_line(p)]
    lead = _lead_index(paras)
    return "\n".join(p for i, p in enumerate(paras) if i == lead or p not in boilerplate)


def _is_lead_like(paragraph):
    paragraph = DATELINE_RE.sub("", paragraph)
    return len(paragraph) >= LEAD_MIN_LEN and re.search(r"[.!?…»)]$", paragraph) is not None


def _lead_index(paras):
    """Номер первого содержательного абзаца (см. pick_lead); None для пустого текста."""
    return next((i for i, p in enumerate(paras) if _is_lead_like(p)), 0 if paras else None)


def split_lead(body_clean):
    """body_clean → (лид, абзацы после лида)."""
    paras = paragraphs(body_clean)
    i = _lead_index(paras)
    if i is None:
        return "", []
    return DATELINE_RE.sub("", paras[i]), paras[i + 1:]


def pick_lead(body_clean):
    """
    Лид — первый содержательный абзац body_clean: не короче LEAD_MIN_LEN символов
    и кончается как предложение. Межзаголовки («Три плюс один») пропускаются,
    подводка агентств («МОСКВА, 19 окт — ПРАЙМ.») срезается. Если такого абзаца нет —
    первый абзац. Используйте его вместо summary: тот совпадает с первым абзацем,
    а иногда и с заголовком.
    """
    return split_lead(body_clean)[0]


# ---------- текст для эмбеддинга ----------

def build_embed_text(title, subtitle, body_clean, limit=EMBED_MAX_CHARS):
    """
    Текст для эмбеддинга (embed_text): заголовок, подзаголовок, лид и следующие абзацы
    body_clean — целыми, пока всё помещается в limit символов. Абзац, который целиком
    не влезает, обрезается по концу предложения, и на нём сборка заканчивается.
    Заголовок попадает целиком всегда.
    """
    lead, rest = split_lead(body_clean)
    text = norm_space(title)
    for part in map(norm_space, [subtitle, lead, *rest]):
        if not part:
            continue
        room = limit - len(text) - 1  # 1 — перевод строки перед абзацем
        if len(part) > room:
            part = _cut(part, room)
            if part:
                text += "\n" + part
            break
        text += "\n" + part
    return text


def _cut(paragraph, room):
    """Обрезает абзац до room символов по концу предложения, а если так остаётся меньше
    MIN_CUT_LEN символов — по границе слова. Если места меньше MIN_CUT_LEN — пустая строка."""
    if room < MIN_CUT_LEN:
        return ""
    head = paragraph[:room]
    end = max((m.end() for m in re.finditer(r"[.!?…]»?(?=\s|$)", head)), default=0)
    if end >= MIN_CUT_LEN:
        return head[:end]
    return head.rsplit(" ", 1)[0]


def content_hash(embed_text):
    """sha256 от embed_text: если хэш поменялся — эмбеддинг новости надо пересчитать."""
    return hashlib.sha256((embed_text or "").encode("utf-8")).hexdigest()
