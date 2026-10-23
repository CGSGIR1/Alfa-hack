"""
Группировка новостей в события (колонка event_id): одно событие часто пишут несколько
изданий или одно издание несколькими заметками.

Сейчас сходство считается по TF-IDF символьных n-грамм заголовка, подзаголовка и лида.
Чтобы перейти на эмбеддинги, достаточно написать класс с теми же двумя методами,
что у TfidfVectorizer:
    encode(texts)    -> список векторов (по одному на текст);
    similarity(a, b) -> косинус двух векторов;
и передать его в cluster_events вместе с порогом, подобранным под эмбеддинги.
Парсер и миграцию при этом менять не нужно.
"""

import math
import re
from collections import Counter
from datetime import timedelta

# Косинус, начиная с которого две новости считаются одним событием.
# Подобран на 99 новостях за 15–20.10.2026: при 0.40 все найденные пары верные,
# при 0.35 появляются ложные склейки (разные законопроекты Госдумы, ФАС).
EVENT_SIM_THRESHOLD = 0.40
# Все новости одного события укладываются в столько часов.
EVENT_WINDOW_HOURS = 48
# Длины символьных n-грамм внутри слова. У «акциз» и «акциза» они почти одинаковые,
# поэтому русская морфология не мешает и стеммер не нужен.
NGRAM_MIN = 3
NGRAM_MAX = 5
# Частые слова, которые ничего не говорят о сути события.
STOP_WORDS = set("""
    и в во не что на с со как а то все так его но к у же за бы по ее было от еще нет о из ему
    когда уже или до для мы их чем без под будет кто этот того этого при об после над через
    эти про всего них это также года году год млн млрд трлн руб рублей тыс
""".split())


def event_text(title, subtitle, lead):
    """Текст, по которому сравниваются новости: заголовок, подзаголовок и лид."""
    return " ".join(part for part in (title, subtitle, lead) if part)


class TfidfVectorizer:
    """TF-IDF по символьным n-граммам слов. Вектор — словарь {n-грамма: вес},
    нормированный на длину 1, поэтому косинус — это просто скалярное произведение."""

    def __init__(self, ngram_min=NGRAM_MIN, ngram_max=NGRAM_MAX, stop_words=STOP_WORDS):
        self.ngram_min = ngram_min
        self.ngram_max = ngram_max
        self.stop_words = stop_words

    def _ngrams(self, text):
        grams = []
        for word in re.findall(r"[a-zа-я]+|\d+(?:[.,]\d+)?", text.lower().replace("ё", "е")):
            if word in self.stop_words:
                continue
            word = f" {word} "  # пробелы по краям отличают начало и конец слова
            for n in range(self.ngram_min, self.ngram_max + 1):
                grams.extend(word[i:i + n] for i in range(len(word) - n + 1))
        return grams

    def encode(self, texts):
        docs = [self._ngrams(t) for t in texts]
        df = Counter(g for doc in docs for g in set(doc))
        n_docs = len(docs)
        vectors = []
        for doc in docs:
            weights = {g: (1 + math.log(c)) * (math.log((1 + n_docs) / (1 + df[g])) + 1)
                       for g, c in Counter(doc).items()}
            norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
            vectors.append({g: w / norm for g, w in weights.items()})
        return vectors

    @staticmethod
    def similarity(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(w * b.get(g, 0.0) for g, w in a.items())


def cluster_events(items, vectorizer=None, threshold=EVENT_SIM_THRESHOLD,
                   window_hours=EVENT_WINDOW_HOURS):
    """
    Делит новости на события.

    items — список (news_id, время публикации — datetime с поясом, текст для сравнения).
    Две новости связываются, если косинус ≥ threshold и между ними не больше window_hours.
    Пары перебираются от самых похожих к менее похожим, и события склеиваются, только
    если всё событие укладывается в window_hours: так ежедневные новости про курс
    доллара не слипаются в одно событие на неделю.

    Возвращает {news_id: event_id}, где event_id — id самой ранней новости события
    (у новости без пары это её собственный id).
    """
    vectorizer = vectorizer or TfidfVectorizer()
    items = sorted(items, key=lambda it: (it[1], it[0]))
    vectors = vectorizer.encode([text for _, _, text in items])
    window = timedelta(hours=window_hours)

    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[j][1] - items[i][1] > window:
                break  # items отсортированы по времени: дальше новости только позже
            sim = vectorizer.similarity(vectors[i], vectors[j])
            if sim >= threshold:
                pairs.append((sim, i, j))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    # union-find: корень события — его самая ранняя новость (наименьший номер в items)
    parent = list(range(len(items)))
    first = [it[1] for it in items]
    last = [it[1] for it in items]

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for _, i, j in pairs:
        a, b = sorted((root(i), root(j)))
        if a == b or max(last[a], last[b]) - min(first[a], first[b]) > window:
            continue
        parent[b] = a
        first[a] = min(first[a], first[b])
        last[a] = max(last[a], last[b])

    return {news_id: items[root(k)][0] for k, (news_id, _, _) in enumerate(items)}
