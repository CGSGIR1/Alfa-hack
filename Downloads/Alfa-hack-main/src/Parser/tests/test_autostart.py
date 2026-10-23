r"""
Тесты автозапуска: что попадает в plist (macOS), в скрипт Планировщика заданий
(Windows) и в crontab (Linux), и как status читает parser.log. Сами планировщики
не трогаются: для cron подставляется ненастоящий crontab.

Запуск из папки Hackaton:
  .venv/bin/python -m unittest discover -s Parser/tests -v        # macOS, Linux
  .venv\Scripts\python -m unittest discover -s Parser\tests -v    # Windows
"""

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# модули парсера лежат на уровень выше папки tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autostart  # noqa: E402

COMMAND = ["/venv/bin/python", "/projects/Hackaton/Parser/parser.py", "--log"]

# Ненастоящий crontab: хранит строки в файле $FAKE_CRONTAB, как настоящий хранит у пользователя.
# С $FAKE_CRONTAB_FAIL отвечает ошибкой, не похожей на «crontab ещё нет».
FAKE_CRONTAB = """#!/bin/sh
if [ -n "$FAKE_CRONTAB_FAIL" ]; then echo "crontab: permission denied" >&2; exit 1; fi
case "$1" in
  -l) if [ -f "$FAKE_CRONTAB" ]; then cat "$FAKE_CRONTAB"; else echo "no crontab for tester" >&2; exit 1; fi ;;
  -) cat > "$FAKE_CRONTAB" ;;
esac
"""

# начало ответа launchctl print: первое «state =» — состояние агента, ниже — вложенные блоки
LAUNCHCTL_PRINT = """gui/501/local.hackaton.news.parser = {
\tactive count = 0
\tpath = /Users/andre/Library/LaunchAgents/local.hackaton.news.parser.plist
\tstate = not running
\truns = 13
\tlast exit code = 0
\tendpoints = {
\t\tstate = active
\t}
}
"""


class LaunchdTest(unittest.TestCase):
    def test_plist(self):
        plist = autostart.launchd_plist(COMMAND)
        self.assertEqual(plist["Label"], "local.hackaton.news.parser")
        self.assertEqual(plist["ProgramArguments"], COMMAND)
        self.assertEqual(plist["StartInterval"], 600)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StandardErrorPath"], str(autostart.LOG_PATH))

    def test_details(self):
        with mock.patch.object(autostart.Launchd, "_info", return_value=LAUNCHCTL_PRINT):
            self.assertEqual(autostart.Launchd.details(), [
                "сейчас ждёт следующего запуска",
                "запусков с момента включения: 13",
                "последний запуск: без ошибок",
            ])


class TaskSchedulerTest(unittest.TestCase):
    def test_register_script(self):
        command = [r"C:\Users\O'Neil\Hackaton\.venv\Scripts\pythonw.exe",
                   r"C:\Users\O'Neil\Hackaton\Parser\parser.py", "--log"]
        script = autostart.task_register_script(command)
        self.assertIn(r"-Execute 'C:\Users\O''Neil\Hackaton\.venv\Scripts\pythonw.exe'", script)
        self.assertIn(r"-Argument 'C:\Users\O''Neil\Hackaton\Parser\parser.py --log'", script)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 10)", script)
        self.assertIn("-AllowStartIfOnBatteries", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        self.assertTrue(script.endswith("Start-ScheduledTask -TaskName 'HackatonNewsParser'"))

    def test_path_with_spaces(self):
        command = [r"C:\Program Files\Python\pythonw.exe", r"C:\My Projects\Parser\parser.py", "--log"]
        script = autostart.task_register_script(command)
        self.assertIn(r"""-Argument '"C:\My Projects\Parser\parser.py" --log'""", script)

    def test_powershell_call(self):
        done = mock.Mock(returncode=0, stdout=b"on", stderr=b"")
        with mock.patch.object(autostart.subprocess, "run", return_value=done) as run:
            self.assertTrue(autostart.TaskScheduler.is_on())
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["powershell", "-NoProfile"])
        script = base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertIn("Get-ScheduledTask -TaskName 'HackatonNewsParser'", script)


class CronLineTest(unittest.TestCase):
    def test_line(self):
        line = autostart.cron_line(["/opt/py 100%/python", "/p/parser.py", "--log"])
        self.assertTrue(line.startswith("*/10 * * * * "))
        self.assertTrue(line.endswith("# hackaton-news-parser"))
        self.assertIn(r"'/opt/py 100\%/python' /p/parser.py --log >> ", line)  # «%» для cron экранирован


@unittest.skipIf(os.name == "nt", "crontab есть только на macOS и Linux")
class CronTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = Path(tmp.name) / "crontab"
        fake.write_text(FAKE_CRONTAB)
        fake.chmod(0o755)
        self.file = Path(tmp.name) / "crontab.txt"
        env = {"PATH": tmp.name + os.pathsep + os.environ["PATH"], "FAKE_CRONTAB": str(self.file)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self):
        return self.file.read_text().splitlines()

    def test_on_keeps_other_lines_and_off_removes_only_ours(self):
        self.file.write_text("0 3 * * * /usr/local/bin/backup.sh\n")
        self.assertFalse(autostart.Cron.is_on())
        autostart.Cron.enable(COMMAND, run_now=False)
        self.assertTrue(autostart.Cron.is_on())
        self.assertEqual(self.lines(), ["0 3 * * * /usr/local/bin/backup.sh", autostart.cron_line(COMMAND)])
        self.assertTrue(autostart.Cron.disable())
        self.assertEqual(self.lines(), ["0 3 * * * /usr/local/bin/backup.sh"])
        self.assertFalse(autostart.Cron.disable())  # второй раз выключать нечего

    def test_first_crontab(self):
        autostart.Cron.enable(COMMAND, run_now=False)  # crontab у пользователя ещё не было
        self.assertEqual(self.lines(), [autostart.cron_line(COMMAND)])

    def test_repeated_on_does_not_duplicate(self):
        autostart.Cron.enable(COMMAND, run_now=False)
        with mock.patch("builtins.print"):
            autostart.cmd_on(autostart.Cron)  # уже включён — ничего не добавляет
        self.assertEqual(self.lines(), [autostart.cron_line(COMMAND)])

    def test_crontab_error_does_not_wipe_lines(self):
        self.file.write_text("0 3 * * * /usr/local/bin/backup.sh\n")
        with mock.patch.dict(os.environ, {"FAKE_CRONTAB_FAIL": "1"}), self.assertRaises(SystemExit):
            autostart.Cron.enable(COMMAND, run_now=False)
        self.assertEqual(self.lines(), ["0 3 * * * /usr/local/bin/backup.sh"])


class LastRunsTest(unittest.TestCase):
    def test_outcomes(self):
        log = "\n".join([
            "===== 2026-10-21 23:14:28 запуск парсера",
            "[vedomosti_business] в ленте: 200",
            "Новых за запуск: 2",
            "Всего в базе: 781, из них с полным текстом: 744. Таблица: news.csv",
            "===== 2026-10-21 23:15:00 запуск парсера",
            "Предыдущий запуск ещё не закончился — этот пропускаю.",
            "===== 2026-10-21 23:25:25 запуск парсера",
            "[kommersant_economics] ошибка: HTTP 503",
            "Новых за запуск: 0",
            "Всего в базе: 781, из них с полным текстом: 744. Таблица: news.csv",
            "===== 2026-10-21 23:35:30 запуск парсера",
            "Traceback (most recent call last):",
        ])
        self.assertEqual(autostart.last_runs(log), [
            "2026-10-21 23:14:28 — новых: 2",
            "2026-10-21 23:15:00 — пропущен: ещё шёл предыдущий запуск",
            "2026-10-21 23:25:25 — новых: 0 (были ошибки, см. parser.log)",
            "2026-10-21 23:35:30 — упал с ошибкой, см. parser.log",
        ])


class FindPythonTest(unittest.TestCase):
    def test_found_python_has_parser_deps(self):
        self.assertTrue(autostart.has_parser_deps(autostart.find_python()))


if __name__ == "__main__":
    unittest.main()
