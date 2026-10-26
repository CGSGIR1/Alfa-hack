from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 300           # модели с рассуждениями отвечают до нескольких минут
JSON_RETRIES = 1
NETWORK_RETRIES = 1
RETRY_PAUSE = 15
RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
_log_lock = threading.Lock()


class LLMError(RuntimeError):
    pass


class LLMResponseError(LLMError):
    """A completed response cannot be repaired by replaying the same request."""


class LLMServiceError(LLMError):
    """Transport/service failure; distinguish outages from configuration failures."""
    def __init__(self,message,retryable=True,status_code=None):
        super().__init__(message)
        self.retryable=retryable
        self.status_code=status_code


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Подхватывает переменные из .env в корне проекта. Уже заданные в окружении не перезаписывает."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        log_file: str | Path | None = None,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.cache_dir = Path(cache_dir or os.environ.get("LLM_CACHE_DIR") or ROOT / ".cache" / "llm")
        self.log_file = Path(log_file or os.environ.get("LLM_LOG_FILE") or self.cache_dir / "calls.jsonl")
        # Расход токенов этим экземпляром (без попаданий в кеш) — для лимитов бюджета.
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        self._thread_usage=threading.local()

    def thread_usage(self):
        """Per-thread counters keep concurrent agent traces from double counting."""
        return dict(getattr(self._thread_usage,'totals',dict(calls=0,prompt_tokens=0,completion_tokens=0,cost=0.0)))

    @staticmethod
    def model(role: str, fallback: str | None = None) -> str:
        """Имя модели по роли из LLM_MODEL_<ROLE>; если не задана — модель роли fallback."""
        name = os.environ.get(f"LLM_MODEL_{role.upper()}")
        if name:
            return name
        if fallback:
            return LLMClient.model(fallback)
        raise LLMServiceError(f"Не задана переменная окружения LLM_MODEL_{role.upper()}",retryable=False)

    def key_info(self) -> dict:
        """Расход и лимит ключа (LiteLLM /key/info): spend, max_budget, budget_reset_at."""
        root = self.base_url.rsplit("/v1", 1)[0]
        request = urllib.request.Request(f"{root}/key/info", headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as r:
                return json.loads(r.read()).get("info") or {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise LLMError(f"Не удалось получить расход ключа: {error}") from error

    def chat_json(self, model: str, messages: list[dict], task: str, temperature: float = 0.2,
                  params: dict | None = None) -> dict:
        """Запрос с ответом в JSON. При невалидном JSON — повтор с указанием ошибки."""
        params={"response_format":{"type":"json_object"},**(params or {})}
        for attempt in range(JSON_RETRIES + 1):
            text = self.chat(model, messages, task, temperature, params)
            try:
                return parse_json(text)
            except ValueError as error:
                if attempt == JSON_RETRIES:
                    raise LLMResponseError(f"Модель вернула невалидный JSON: {error}") from error
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Ответ не является валидным JSON ({error}). Верни только JSON."},
                ]
        raise AssertionError("unreachable")

    def chat(self, model: str, messages: list[dict], task: str, temperature: float = 0.2,
             params: dict | None = None) -> str:
        """params — дополнительные поля запроса API (например, max_tokens); входят в ключ кеша."""
        payload = {"model": model, "messages": messages, "temperature": temperature, **(params or {})}
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cached = self.cache_dir / f"{key}.json"
        if cached.exists():
            response = json.loads(cached.read_text(encoding='utf-8'))
            self._log(task, model, response, 0.0, cache_hit=True)
            return _content(response)

        if not self.base_url or not self.api_key:
            raise LLMServiceError("Не заданы LLM_BASE_URL и LLM_API_KEY",retryable=False)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        started = time.monotonic()
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                    response = json.loads(r.read())
                break
            except urllib.error.HTTPError as error:
                # 5xx и 429 — временные проблемы прокси, 4xx (включая исчерпанный бюджет) — нет
                if error.code in RETRY_HTTP_CODES and attempt < NETWORK_RETRIES:
                    time.sleep(RETRY_PAUSE)
                    continue
                raise LLMServiceError(f"HTTP {error.code}: {error.read().decode(errors='replace')[:500]}",
                                      retryable=error.code in RETRY_HTTP_CODES,status_code=error.code) from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < NETWORK_RETRIES:
                    time.sleep(RETRY_PAUSE)
                    continue
                raise LLMServiceError(f"Нет связи с API: {error}") from error

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._log(task, model, response, time.monotonic() - started, cache_hit=False)
        content = _content(response)
        cached.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
        return content

    def _log(self, task: str, model: str, response: dict, seconds: float, cache_hit: bool) -> None:
        usage = response.get("usage") or {}
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "task": task,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cost": usage.get("cost"),  # стоимость, которую возвращает API организаторов
            "seconds": round(seconds, 2),
            "cache_hit": cache_hit,
        }
        with _log_lock:
            if not cache_hit:
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += usage.get("prompt_tokens") or 0
                self.usage["completion_tokens"] += usage.get("completion_tokens") or 0
                self.usage["cost"] += usage.get("cost") or 0.0
                totals=self.thread_usage()
                for key in ('prompt_tokens','completion_tokens','cost'):totals[key]+=usage.get(key) or 0
                totals['calls']+=1
                self._thread_usage.totals=totals
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _content(response: dict) -> str:
    try:
        choice = response["choices"][0]
        if choice['message'].get('refusal'):
            raise LLMResponseError('Модель отказалась обрабатывать вход: '+str(choice['message']['refusal'])[:500])
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            reason = choice.get('finish_reason')
            raise LLMResponseError(f"Модель не вернула текст ответа (finish_reason={reason}); при length увеличьте лимит или отключите рассуждения")
        return content
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise LLMResponseError(f"Неожиданный формат ответа: {str(response)[:300]}") from error


def parse_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа: модели часто оборачивают его в ```json ... ``` или пишут пояснения."""
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = match.group(1) if match else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON-объекта")
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as error:
        raise ValueError(str(error)) from error
