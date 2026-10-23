# Парсер деловых новостей

Парсер собирает новости Коммерсанта и Ведомостей из RSS в SQLite (`news.db`) и готовит их к разметке:
- чистит текст;
- выделяет лид;
- собирает текст для эмбеддинга;
- объединяет перепечатки одного события.

Подробности — в начале `parser.py`. Работает на Windows, macOS и Linux.

## Установка

Нужен Python 3.8 или новее (проверено на 3.14). Команды выполняются из папки `Hackaton`, где лежат `Parser` и окружение `.venv`.

macOS и Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r Parser/requirements.txt
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\pip install -r Parser\requirements.txt
```

## Запуск вручную

| | macOS, Linux | Windows |
|---|---|---|
| парсер | `.venv/bin/python Parser/parser.py` | `.venv\Scripts\python Parser\parser.py` |
| тесты | `.venv/bin/python -m unittest discover -s Parser/tests -v` | `.venv\Scripts\python -m unittest discover -s Parser\tests -v` |

## Автозапуск каждые 10 минут

Команды одинаковые на всех ОС. Подойдёт любой Python 3: на macOS и Linux — `python3`, на Windows — `py`. Python с зависимостями для самого парсера скрипт найдёт сам.

```bash
python3 Parser/autostart.py on       # включить: первый запуск сразу, дальше каждые 10 минут
python3 Parser/autostart.py off      # выключить совсем, в том числе после перезагрузки
python3 Parser/autostart.py status   # включён ли, как прошли последние запуски, что в базе
```

| ОС | Чем запускается | Что учесть |
|---|---|---|
| macOS | launchd: `~/Library/LaunchAgents/local.hackaton.news.parser.plist` | работает, пока вы вошли в систему; после сна сразу делает пропущенный запуск |
| Windows | Планировщик заданий: задача `HackatonNewsParser` | работает, пока вы вошли в систему; окно консоли не появляется; запускается и от батареи. Если Windows ответит «Отказано в доступе», запустите терминал от имени администратора |
| Linux | cron: строка с пометкой `# hackaton-news-parser` | работает и без входа в систему, если запущен демон cron; пропущенные запуски не догоняет |

Вывод каждого запуска дописывается в `Parser/parser.log`. Два запуска одновременно не работают: второй видит блокировку `parser.lock` и пропускает свою очередь.

## Файлы

**Данные:**
- `news.db` — база;
- `news.csv` — её полная копия для Excel;
- `parser.log` — лог запусков;
- `parser.lock` — блокировка.

**Код:**
- `parser.py` — сбор новостей;
- `cleaning.py` — очистка текста;
- `events.py` — группировка новостей в события;
- `migrate.py` — перевод старой базы на новые колонки (`--report` показывает отчёт, ничего не меняя);
- `autostart.py` — автозапуск.
