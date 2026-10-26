FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEWS_ANALYTICS_HOME=/app EMBEDDING_MODEL_PATH=/models/embeddinggemma-300m \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY app.py config.json ./
COPY src ./src
# Allowlist runtime code; never COPY the prepared data or module examples.
COPY company-news-analytics/news_analytics ./company-news-analytics/news_analytics
COPY company-news-analytics/prompts ./company-news-analytics/prompts
COPY company-news-analytics/config.json ./company-news-analytics/config.json
COPY THIRD_PARTY_NOTICES.md ./
RUN mkdir -p runtime /models
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=5).close()"
ENTRYPOINT ["python", "app.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
