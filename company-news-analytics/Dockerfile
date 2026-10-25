FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt
COPY news_analytics ./news_analytics
COPY run.py start_ui.py config.json ./
COPY prompts ./prompts
COPY data/input ./data/input
ENV EMBEDDING_MODEL_PATH=/models/embeddinggemma-300m
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 8765
ENTRYPOINT ["python", "run.py"]
CMD ["status"]
