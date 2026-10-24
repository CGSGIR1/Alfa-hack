# Внешние технические источники

Конфигурация сверена с первичными источниками при подготовке поставки:

- [Docker Compose: сервисы и длинный синтаксис volumes](https://docs.docker.com/reference/compose-file/services/#volumes) — подключение модели только для чтения и запрет создания отсутствующей папки модели.
- [actions/checkout](https://github.com/actions/checkout) и [actions/setup-python](https://github.com/actions/setup-python) — используемые действия v7 для GitHub-hosted runners.
- [CPU-индекс PyTorch](https://download.pytorch.org/whl/cpu/torch/) — наличие колёс torch 2.14.1+cpu для Python 3.12 Windows/Linux x86-64.
- [PyPI](https://pypi.org/) — доступность остальных точных версий из requirements.lock.

Это сверка форматов и доступности. Локальная сборка контейнера не выполнялась:
Docker на машине проверки отсутствует. В репозитории подготовлен отдельный CI-job сборки.
