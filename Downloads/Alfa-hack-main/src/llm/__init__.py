"""Клиент языковых моделей для всех компонентов решения.

OpenAI-совместимый API (POST {base_url}/chat/completions), только стандартная библиотека.
Настройка через переменные окружения или файл .env в корне проекта (шаблон — .env.example):

    LLM_BASE_URL   адрес API, например https://.../v1
    LLM_API_KEY    ключ доступа
    LLM_MODEL_FAST модель для простых задач (классификация, переформулировка)
    LLM_MODEL_MAIN модель для анализа и синтеза
    LLM_MODEL_QUERIES модель для запросов влияния (по умолчанию LLM_MODEL_MAIN)
    LLM_CACHE_DIR  кеш ответов (по умолчанию .cache/llm)
    LLM_LOG_FILE   журнал вызовов с расходом токенов (по умолчанию .cache/llm/calls.jsonl)

Каждый вызов журналируется: модель, задача, токены, время, попадание в кеш.
"""

from .client import LLMClient, LLMError, LLMUnavailable

__all__ = ["LLMClient", "LLMError", "LLMUnavailable"]
