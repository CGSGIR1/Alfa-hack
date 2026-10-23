"""
Автозапуск парсера каждые 10 минут — одни и те же команды на macOS, Windows и Linux:

  python autostart.py on       # включить: первый запуск сразу, дальше каждые 10 минут
  python autostart.py off      # выключить совсем, в том числе после перезагрузки
  python autostart.py status   # включён ли, как прошли последние запуски, что в базе

Скрипту хватает стандартной библиотеки, запускать его можно любым Python 3.
Для самого парсера он находит Python, в котором стоят зависимости из requirements.txt.

Чем запускается парсер по расписанию:
  macOS   — launchd: ~/Library/LaunchAgents/local.hackaton.news.parser.plist;
  Windows — Планировщик заданий: задача HackatonNewsParser (pythonw, без окна консоли);
  Linux   — cron: строка с пометкой «# hackaton-news-parser» в crontab.
Каждый запуск парсер сам дописывает в parser.log (флаг --log).
"""

import argparse
import base64
import json
import os
import plistlib
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

INTERVAL_MIN = 10  # как часто запускать парсер, минут
BASE_DIR = Path(__file__).resolve().parent
PARSER = BASE_DIR / "parser.py"
LOG_PATH = BASE_DIR / "parser.log"
DB_PATH = BASE_DIR / "news.db"
# без этих пакетов парсер не запустится (см. requirements.txt)
REQUIRED_MODULES = ("feedparser", "requests", "trafilatura")

LABEL = "local.hackaton.news.parser"  # macOS: метка агента launchd
TASK_NAME = "HackatonNewsParser"  # Windows: имя задачи в Планировщике заданий
CRON_MARK = "# hackaton-news-parser"  # Linux: пометка нашей строки в crontab


# ---------- чем запускать парсер ----------

def venv_python(venv):
    """Python внутри окружения venv: на Windows и на macOS/Linux он лежит по-разному."""
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def has_parser_deps(python):
    """True, если в этом Python импортируются зависимости парсера."""
    try:
        check = subprocess.run([str(python), "-c", "import " + ", ".join(REQUIRED_MODULES)],
                               capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0


def find_python():
    """
    Python, которым планировщик будет запускать парсер: тот, что запустил этот скрипт,
    или окружение .venv в папке парсера или уровнем выше — первый, где стоят зависимости.
    На Windows берётся соседний pythonw.exe: он не открывает окно консоли.
    """
    candidates = [Path(sys.executable), venv_python(BASE_DIR / ".venv"), venv_python(BASE_DIR.parent / ".venv")]
    for python in dict.fromkeys(candidates):
        if python.exists() and has_parser_deps(python):
            pythonw = python.with_name("pythonw.exe")
            return pythonw if os.name == "nt" and pythonw.exists() else python
    sys.exit("Не нашёл Python с зависимостями парсера. Поставьте их и запустите снова:\n"
             f"  python -m pip install -r {BASE_DIR / 'requirements.txt'}")


def parser_command(python):
    """Команда, которую планировщик выполняет каждые INTERVAL_MIN минут."""
    return [str(python), str(PARSER), "--log"]


# ---------- macOS: launchd ----------

def launchd_plist(command):
    """Описание агента launchd: запуск каждые INTERVAL_MIN минут и сразу после включения."""
    return {
        "Label": LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(BASE_DIR),
        "StartInterval": INTERVAL_MIN * 60,
        "RunAtLoad": True,
        # вывод парсер пишет сам (--log); сюда попадут только ошибки до его старта
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }


class Launchd:
    """macOS: агент launchd в ~/Library/LaunchAgents (работает, пока вы вошли в систему)."""

    name = "launchd на macOS"

    @staticmethod
    def _job():
        return f"gui/{os.getuid()}/{LABEL}"

    @staticmethod
    def _plist():
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @classmethod
    def _info(cls):
        """Вывод launchctl print или None, если агент не загружен."""
        result = subprocess.run(["launchctl", "print", cls._job()], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None

    @classmethod
    def is_on(cls):
        return cls._info() is not None

    @classmethod
    def enable(cls, command):
        plist = cls._plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps(launchd_plist(command)))
        subprocess.run(["launchctl", "enable", cls._job()], check=True)  # снять отметку «выключен», если была
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)

    @classmethod
    def disable(cls):
        was_on = cls.is_on()
        if was_on:
            subprocess.run(["launchctl", "bootout", cls._job()], check=True)
        cls._plist().unlink(missing_ok=True)  # без файла агент не загрузится и после входа в систему
        return was_on

    @classmethod
    def details(cls):
        lines = (cls._info() or "").splitlines()

        def field(name):  # первое вхождение — состояние самого агента, ниже идут вложенные блоки
            return next((ln.split(" = ", 1)[1] for ln in lines if ln.strip().startswith(name + " = ")), "")

        state, runs, code = field("state"), field("runs"), field("last exit code")
        if code == "0":
            last = "без ошибок"
        elif code in ("", "(never exited)"):
            last = "ещё идёт"
        else:
            last = f"завершился с ошибкой (код {code}), подробности в parser.log"
        return [
            "сейчас парсер работает" if state == "running" else "сейчас ждёт следующего запуска",
            f"запусков с момента включения: {runs or 0}",
            f"последний запуск: {last}",
        ]


# ---------- Windows: Планировщик заданий ----------

def ps_quote(value):
    """Строка в одинарных кавычках PowerShell: апостроф внутри удваивается."""
    return "'" + str(value).replace("'", "''") + "'"


def task_register_script(command):
    """
    Скрипт PowerShell, который создаёт задачу и сразу её запускает. Задача повторяется
    каждые INTERVAL_MIN минут, работает и от батареи, после сна выполняет пропущенный
    запуск сразу и не стартует, пока не закончился предыдущий.
    """
    executable, arguments = command[0], subprocess.list2cmdline(command[1:])
    return "\n".join([
        f"$action = New-ScheduledTaskAction -Execute {ps_quote(executable)} "
        f"-Argument {ps_quote(arguments)} -WorkingDirectory {ps_quote(BASE_DIR)}",
        f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {INTERVAL_MIN}) -RepetitionDuration (New-TimeSpan -Days 3650)",
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)",
        f"Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $action -Trigger $trigger "
        "-Settings $settings -Force | Out-Null",
        f"Start-ScheduledTask -TaskName {ps_quote(TASK_NAME)}",
    ])


TASK_INFO_SCRIPT = f"""
$task = Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)}
$info = $task | Get-ScheduledTaskInfo
[pscustomobject]@{{
    State = "$($task.State)"
    LastRunTime = $(if ($info.LastRunTime) {{ $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ '' }})
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $(if ($info.NextRunTime) {{ $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ '' }})
}} | ConvertTo-Json -Compress
"""

# коды LastTaskResult, которые означают не ошибку, а состояние задачи
TASK_RESULTS = {0: "без ошибок", 267009: "идёт сейчас", 267011: "ещё не запускался",
                267014: "прерван: задачу остановили"}


def powershell(script):
    """Выполняет скрипт PowerShell и возвращает его вывод. При ошибке — выход с её текстом."""
    script = ("$ErrorActionPreference = 'Stop'\n"
              "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}\n" + script)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # кавычки в путях не ломаются
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-EncodedCommand", encoded], capture_output=True)
    if result.returncode != 0:
        sys.exit("Планировщик заданий ответил ошибкой:\n" + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("utf-8", "replace").strip()


class TaskScheduler:
    """Windows: задача в Планировщике заданий (работает, пока вы вошли в систему)."""

    name = "Планировщик заданий Windows"

    @staticmethod
    def is_on():
        task = f"Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue"
        return powershell(f"if ({task}) {{ 'on' }}") == "on"

    @staticmethod
    def enable(command):
        powershell(task_register_script(command))

    @classmethod
    def disable(cls):
        was_on = cls.is_on()
        if was_on:
            powershell(f"Unregister-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Confirm:$false")
        return was_on

    @staticmethod
    def details():
        info = json.loads(powershell(TASK_INFO_SCRIPT))
        code = info["LastTaskResult"]
        result = TASK_RESULTS.get(code, f"ошибка (код {code}), подробности в parser.log")
        when = "—" if code == 267011 else info["LastRunTime"] or "—"  # у незапускавшейся задачи дата 1999 года
        return [
            "сейчас парсер работает" if info["State"] == "Running" else "сейчас ждёт следующего запуска",
            f"последний запуск: {when}, {result}",
            f"следующий запуск: {info['NextRunTime'] or '—'}",
        ]


# ---------- Linux: cron ----------

def cron_line(command):
    """Строка crontab: каждые INTERVAL_MIN минут. Ошибки до старта парсера (например,
    не хватает зависимостей) cron допишет в тот же лог. «%» для cron — особый знак."""
    shell_command = shlex.join(command) + f" >> {shlex.quote(str(LOG_PATH))} 2>&1"
    shell_command = shell_command.replace("%", r"\%")
    return f"*/{INTERVAL_MIN} * * * * {shell_command} {CRON_MARK}"


def read_crontab():
    """Строки crontab текущего пользователя; пустой список, если crontab ещё не создан."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("Не найдена программа crontab: установите cron (пакет cron или cronie).")
    if result.returncode == 0:
        return result.stdout.splitlines()
    if "no crontab" in result.stderr.lower():
        return []
    # любую другую ошибку не считаем пустым crontab — иначе при записи стёрли бы чужие строки
    sys.exit("crontab ответил ошибкой: " + result.stderr.strip())


def write_crontab(lines):
    subprocess.run(["crontab", "-"], input="".join(line + "\n" for line in lines), text=True, check=True)


class Cron:
    """Linux (и другие Unix без launchd): строка в crontab пользователя. Работает без входа
    в систему, если запущен демон cron."""

    name = "cron на Linux"

    @staticmethod
    def _ours(line):
        return line.rstrip().endswith(CRON_MARK)

    @classmethod
    def is_on(cls):
        return any(cls._ours(line) for line in read_crontab())

    @staticmethod
    def enable(command, run_now=True):
        write_crontab(read_crontab() + [cron_line(command)])
        if run_now:  # первый запуск сразу, как на macOS и Windows; дальше — по расписанию cron
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)

    @classmethod
    def disable(cls):
        lines = read_crontab()
        kept = [line for line in lines if not cls._ours(line)]
        if len(kept) != len(lines):
            write_crontab(kept)
        return len(kept) != len(lines)

    @classmethod
    def details(cls):
        line = next(line for line in read_crontab() if cls._ours(line))
        return [f"строка в crontab: {line}", "журнал запусков cron не ведёт — смотрите последние запуски ниже"]


def backend():
    """Планировщик этой ОС."""
    if sys.platform == "darwin":
        return Launchd
    if os.name == "nt":
        return TaskScheduler
    return Cron


# ---------- проверка: лог и база ----------

def read_log_tail(size=256 * 1024):
    """Конец parser.log (последние size байт) — лог растёт, весь читать незачем."""
    if not LOG_PATH.exists():
        return ""
    with open(LOG_PATH, "rb") as f:
        f.seek(max(0, LOG_PATH.stat().st_size - size))
        return f.read().decode("utf-8", "replace")


def last_runs(log_text, limit=6):
    """Последние запуски из лога парсера: время и чем закончился каждый."""
    runs = []
    for line in log_text.splitlines():
        if line.startswith("===== "):
            when = line[len("===== "):].replace(" запуск парсера", "")
            runs.append({"when": when, "new": None, "done": False, "skipped": False, "errors": False})
        elif not runs:
            continue
        elif line.startswith("Новых за запуск:"):
            runs[-1]["new"] = line.split(":", 1)[1].strip()
        elif line.startswith("Всего в базе:"):
            runs[-1]["done"] = True
        elif "пропускаю" in line:
            runs[-1]["skipped"] = True
        elif line.startswith("Traceback") or "ошибка:" in line:
            runs[-1]["errors"] = True

    def outcome(run):
        if run["skipped"]:
            return "пропущен: ещё шёл предыдущий запуск"
        if run["done"]:
            text = f"новых: {run['new']}" if run["new"] is not None else "готово"
            return text + (" (были ошибки, см. parser.log)" if run["errors"] else "")
        return "упал с ошибкой, см. parser.log" if run["errors"] else "идёт сейчас или прервался"

    return [f"{run['when']} — {outcome(run)}" for run in runs[-limit:]]


def local_time(value):
    """Время из базы (UTC) → местное, «21.10 11:48»."""
    return datetime.fromisoformat(value).astimezone().strftime("%d.%m %H:%M") if value else "—"


def db_summary():
    """Короткая сводка по news.db."""
    if not DB_PATH.exists():
        return ["базы ещё нет — она появится после первого запуска"]
    db = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    total, full = db.execute("SELECT COUNT(*), SUM(is_full = 1) FROM news").fetchone()
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    fresh = db.execute("SELECT COUNT(*) FROM news WHERE parsed_at >= ?", (day_ago,)).fetchone()[0]
    newest = db.execute("SELECT MAX(published_utc) FROM news").fetchone()[0]
    feeds = db.execute("SELECT source, COUNT(*) FROM news GROUP BY source ORDER BY source").fetchall()
    reasons = db.execute(
        "SELECT CASE WHEN fetch_error LIKE 'пейволл%' THEN 'пейволл' ELSE fetch_error END, COUNT(*) "
        "FROM news WHERE is_full = 0 GROUP BY 1 ORDER BY 2 DESC").fetchall()
    latest = db.execute("SELECT published_utc, outlet, title FROM news ORDER BY published_utc DESC LIMIT 5").fetchall()
    db.close()
    lines = [
        f"новостей: {total}, с полным текстом: {full or 0}",
        f"добавлено за последние 24 часа: {fresh}",
        f"самая свежая публикация: {local_time(newest)}",
        "по лентам: " + ", ".join(f"{source} — {n}" for source, n in feeds),
    ]
    if reasons:
        lines.append("неполный текст: " + ", ".join(f"{reason} — {n}" for reason, n in reasons))
    lines.append("последние новости:")
    lines += [f"  {local_time(published)}  [{outlet}]  {title}" for published, outlet, title in latest]
    return lines


# ---------- команды ----------

def cmd_on(scheduler):
    if scheduler.is_on():
        print("Автопарсинг уже включён. Проверить: python autostart.py status")
        return
    scheduler.enable(parser_command(find_python()))
    print(f"Автопарсинг включён ({scheduler.name}): парсер запускается каждые {INTERVAL_MIN} минут, "
          "первый запуск — прямо сейчас.")
    print(f"Лог: {LOG_PATH}")


def cmd_off(scheduler):
    if scheduler.disable():
        print("Автопарсинг выключен.")
    else:
        print("Автопарсинг и так был выключен.")


def cmd_status(scheduler):
    print(f"=== Автопарсинг — {scheduler.name}")
    if scheduler.is_on():
        print(f"включён, запуск каждые {INTERVAL_MIN} минут")
        for line in scheduler.details():
            print("  " + line)
    else:
        print("выключен (включить: python autostart.py on)")
    print("\n=== Последние запуски (parser.log)")
    for line in last_runs(read_log_tail()) or ["запусков ещё не было"]:
        print("  " + line)
    print("\n=== База news.db")
    for line in db_summary():
        print("  " + line)


def main():
    commands = {"on": cmd_on, "off": cmd_off, "status": cmd_status}
    ap = argparse.ArgumentParser(description="Автозапуск парсера каждые 10 минут (macOS, Windows, Linux)")
    ap.add_argument("command", choices=commands, help="on — включить, off — выключить, status — проверить")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows при выводе в файл берёт cp1251, где нет части знаков
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        commands[args.command](backend())
    except subprocess.CalledProcessError as e:  # текст ошибки launchctl или crontab уже напечатан выше
        sys.exit(f"Не получилось: команда {' '.join(map(str, e.cmd))} завершилась с кодом {e.returncode}.")


if __name__ == "__main__":
    main()
