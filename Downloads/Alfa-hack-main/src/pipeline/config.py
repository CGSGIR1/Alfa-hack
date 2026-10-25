"""Настройки пайплайна. Пути и сроки в одном месте, чтобы их было видно и легко менять."""

from __future__ import annotations

from pathlib import Path

from ..big_business import BIG, BIG_REVIEW, BIG_VIA_GROUP, Config as ClassifierConfig

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
APP_DB = DATA / "app.db"                      # компании, новости, показатели, отчёты
PARSER_DB = ROOT / "src" / "parser" / "news.db"
REPORTS_DIR = DATA / "reports"                # готовые отчёты для фронтенда: <company_id>.json
ANALYTICS_STUB = DATA / "analytics_stub.json" # временный корпус аналитики до модуля аналитики

# Кто пользователь пайплайна: клиент, которого классификатор крупного бизнеса относит к этим категориям
# (подробности и числа: https://claude.ai/artifact/Eh1uD5aiWD2htBUeoSTYRC):
#   BIG           выручка компании > 15 млрд ₽;
#   BIG_REVIEW    то же, но госсобственность или < 15 сотрудников (пускаем без пометки);
#   BIG_VIA_GROUP своя выручка <= 15 млрд ₽, но у группы > 15 млрд ₽ и у самой компании не менее 2 млрд ₽.
# Остальные категории (BORDERLINE, NOT_BIG, EXCLUDED, INVALID_DATA) не получают ни отчётов, ни переключателя.
# Чтобы сузить или расширить круг — поменять include_categories здесь; всё остальное подстроится.
CLASSIFIER = ClassifierConfig(include_categories=frozenset({BIG, BIG_REVIEW, BIG_VIA_GROUP}))

# Хранение новостей: важные дольше, незначительные меньше (дней от публикации).
RETENTION_DAYS = {"high": 30, "normal": 10, "low": 3}

# Сколько дней (от публикации) помнить id удалённых по сроку новостей, чтобы не принять их за новые.
# Должно быть заметно больше окна приёма RETRIEVAL["news_days"].
EXPIRED_MEMORY_DAYS = 30

# Повторы разметки новости, на которой модель споткнулась (невалидный ответ, отказ и т. п.):
# после 1-го сбоя ждём 1 ч, после 2-го — 6 ч, после 3-го новость больше не берём.
INGEST_MAX_ATTEMPTS = 3
INGEST_BACKOFF_HOURS = (1, 6)
# Если за один запуск API недоступно столько раз, остальные запросы не отправляем (ключ, бюджет, сеть).
INGEST_ABORT_AFTER_UNAVAILABLE = 5

# Отбор материалов для пакета (как в report-generation-input-1.0).
RETRIEVAL = {
    "news_days": 10,
    "top_news": 3,
    "news_fragments": 2,
    "analytics_per_news": 5,
    "max_chunks_per_analytics": 2,
    "max_analytics_age_days": 365,
    # Пороги для заглушки эмбеддингов (символьные n-граммы); для настоящих эмбеддингов подобрать заново.
    "min_news_score": 0.12,
    "min_analytics_score": 0.10,
}

# Обновление отчёта: раз в сутки, либо раньше — если в топ-N фрагментов пришёл новый.
REPORT_MAX_AGE_HOURS = 24
TRIGGER_TOP_N = 5
