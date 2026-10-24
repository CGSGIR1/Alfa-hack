# Внешняя модель

Весов EmbeddingGemma и обученных адаптеров в репозитории нет.

Разместите полную уже скачанную папку `google/embeddinggemma-300m` в
`models/embeddinggemma-300m/` либо укажите другой путь:

- обычный Python: `model_path` в `config.json`, `--model` или
  переменная `EMBEDDING_MODEL_PATH`;
- Docker Compose: `MODEL_HOST_PATH` в локальном `.env`.

Нужны все штатные файлы модели, включая подпапки Pooling/Dense и токенизатор,
а не один `model.safetensors`. Загрузка из интернета во время инференса выключена.
Файлы этой папки, кроме README, исключены из Git и Docker build context.
