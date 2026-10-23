"""Проверка и исправление цитат без LLM, расчёт покрытия исходного текста.

Модели (особенно небольшие) склеивают несмежные отрывки, правят кавычки и пробелы
или слегка пересказывают цитату. Каждая цитата приводится к точному отрывку
исходного блока:
1. точное вхождение — оставляем;
2. совпадение после нормализации пробелов и кавычек — берём оригинальный отрывок;
3. иначе режем цитату на предложения и для каждого ищем ближайший отрывок блока
   (difflib); принимаем при сходстве не ниже FUZZY_MIN_RATIO;
4. если ничего не нашлось — цитата удаляется, это попадает в отчёт.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

FUZZY_MIN_RATIO = 0.85
MIN_SENTENCE_CHARS = 25   # короче — служебные обрывки, их покрытие не проверяем
QUOTE_CHARS = {"“": '"', "”": '"', "«": '"', "»": '"', "„": '"', "‘": "'", "’": "'", "\xa0": " "}

_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"»”]?\s+(?=[\"«“(]?[А-ЯЁA-Z0-9])")


def _norm_char(c: str) -> str:
    return QUOTE_CHARS.get(c, c)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Нормализованный текст и индекс исходного символа для каждого символа результата."""
    out, index, prev_space = [], [], False
    for i, ch in enumerate(text):
        ch = _norm_char(ch)
        if ch.isspace():
            if prev_space:
                continue
            ch, prev_space = " ", True
        else:
            prev_space = False
        out.append(ch)
        index.append(i)
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0].strip()


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


@dataclass
class QuoteFix:
    original: str
    status: str                                   # exact | normalized | fuzzy | dropped
    spans: list[tuple[str, str]] = field(default_factory=list)  # (block_id, точный отрывок)
    ratio: float | None = None


def _find_normalized(quote: str, block: str) -> str | None:
    norm_block, index = normalize_with_map(block)
    q = normalize(quote)
    pos = norm_block.find(q)
    if not q or pos == -1:
        return None
    return block[index[pos]: index[pos + len(q) - 1] + 1]


def _best_span(piece: str, block: str) -> tuple[str, float]:
    """Ближайший к piece отрывок блока: якорь по самому длинному совпадению, затем подгонка границ."""
    matcher = SequenceMatcher(None, block, piece, autojunk=False)
    m = matcher.find_longest_match(0, len(block), 0, len(piece))
    if m.size == 0:
        return "", 0.0
    start = max(0, m.a - m.b)
    end = min(len(block), start + len(piece))
    best, best_ratio = block[start:end], 0.0
    # подвигаем границы к ближайшим пробелам/концам предложений, выбирая лучший вариант
    for ds in range(-15, 16, 3):
        for de in range(-15, 16, 3):
            s, e = max(0, start + ds), min(len(block), end + de)
            if e - s < len(piece) * 0.6:
                continue
            candidate = block[s:e].strip(" ,;")
            ratio = SequenceMatcher(None, normalize(candidate), normalize(piece), autojunk=False).ratio()
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
    return best, best_ratio


def fix_quote(quote: str, blocks: dict[str, str], preferred: list[str] | None = None) -> QuoteFix:
    order = [b for b in (preferred or []) if b in blocks] + [b for b in blocks if b not in (preferred or [])]
    for block_id in order:
        if quote and quote in blocks[block_id]:
            return QuoteFix(quote, "exact", [(block_id, quote)])
    for block_id in order:
        found = _find_normalized(quote, blocks[block_id])
        if found:
            return QuoteFix(quote, "normalized", [(block_id, found)])

    spans, ratios = [], []
    for piece in split_sentences(quote) or [quote]:
        if len(piece) < 10:
            continue
        candidates = [(*_best_span(piece, blocks[b]), b) for b in order]
        span, ratio, block_id = max(candidates, key=lambda c: c[1], default=("", 0.0, None))
        if ratio >= FUZZY_MIN_RATIO and span and (block_id, span) not in spans:
            spans.append((block_id, span))
            ratios.append(ratio)
    if spans:
        return QuoteFix(quote, "fuzzy", spans, round(min(ratios), 3))
    return QuoteFix(quote, "dropped")


def uncovered_sentences(blocks: dict[str, str], quotes: list[str]) -> list[tuple[str, str]]:
    """Предложения блоков, которые не входят ни в одну цитату (по нормализованному тексту)."""
    normalized_quotes = [normalize(q) for q in quotes]
    result = []
    for block_id, text in blocks.items():
        for sentence in split_sentences(text):
            if len(sentence) < MIN_SENTENCE_CHARS:
                continue
            s = normalize(sentence)
            # предложение покрыто, если целиком входит в цитату или цитата покрывает его большую часть
            if any(s in q or (len(q) >= 0.6 * len(s) and q in s) for q in normalized_quotes):
                continue
            result.append((block_id, sentence))
    return result
