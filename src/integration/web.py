from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import os
import threading
import time
import traceback
import webbrowser
from uuid import uuid4

from news_analytics.web import Application as SearchApplication, Handler as SearchHandler
from .service import Service
from .admission import policy


class Application(SearchApplication):
    operations = SearchApplication.operations | {'generate', 'tick', 'select-company', 'auto-poll', 'add-company'}

    def __init__(self, config_path, config):
        super().__init__(config_path)
        self.service = Service(config)
        self.service.cleanup()
        self.stop = threading.Event()
        self.poll_wake = threading.Event()
        self.profile_pending = threading.Event()
        self.poll_settings_lock = threading.Lock()
        self.auto_poll = os.environ.get('PIPELINE_AUTO_POLL', '1') == '1'
        self.collect_wake = threading.Event()
        self.collection = dict(status='idle', phase='Ожидаем проверки источников')
        self.next_process_delay = 1
        from .currency import CurrencyRates
        self.currency_rates = CurrencyRates(self.service.db.parent / 'cache' / 'alfa-currency.json')

    def start(self, kind, payload):
        if kind == 'select-company':
            result = self.service.select_company(payload.get('company_id'))
            self.poll_wake.set()
            return result
        if kind == 'auto-poll':
            return self.configure_poll(payload.get('enabled'))
        if kind == 'add-company':
            from .onboarding import company_id
            from news_analytics.common import require
            identifier = company_id(payload.get('company_id'))
            with self.lock:
                pending = [j for j in self.jobs.values() if j['status'] == 'running']
                same = next((j for j in pending if j['kind'] == kind and j.get('company_id') == identifier), None)
                if same:
                    return dict(job_id=same['id'])
                require(sum(j['kind'] == kind for j in pending) < 5, 'Дождитесь подготовки уже добавляемых компаний.')
                # Queue behind a running report instead of rejecting the user
                # or competing for the same embedding model/SQLite writer.
                jid = uuid4().hex
                for key in list(self.jobs):
                    if len(self.jobs) < 20:
                        break
                    if self.jobs[key]['status'] != 'running':
                        del self.jobs[key]
                self.jobs[jid] = dict(id=jid, kind=kind, company_id=identifier, status='running',
                                      phase='Ожидаем завершения текущей обработки…' if pending else 'Подготавливаем компанию…')
                self.active = jid
                self.profile_pending.set()
            self.pool.submit(self._work, jid, kind, dict(company_id=identifier))
            return dict(job_id=jid)
        with self.lock:
            if any(j['status'] == 'running' and j['kind'] == 'add-company' for j in self.jobs.values()):
                raise BlockingIOError('Дождитесь подготовки компании.')
        return super().start(kind, payload)

    def state(self):
        result = super().state()
        result['integration'] = self.service.status()
        result['integration']['auto_poll'] = self.auto_poll
        result['integration']['collection'] = dict(self.collection)
        result['integration']['can_add_company'] = True
        return result

    def configure_poll(self, enabled):
        from news_analytics.common import require
        from .settings import llm_ready, save_auto_poll
        require(type(enabled) is bool, 'Укажите enabled: true или false')
        if enabled:
            require(llm_ready(), 'Сначала настройте доступ к LLM')
        with self.poll_settings_lock:
            save_auto_poll(enabled)
            self.auto_poll = enabled
            self.poll_wake.set()
            self.collect_wake.set()
        return dict(auto_poll=enabled, running_job_continues=True)

    def _work(self, jid, kind, payload):
        if kind == 'add-company':
            try:
                result = self.service.add_company(payload['company_id'],
                    progress=lambda phase: self._update(jid, phase=phase))
                self._update(jid, status='complete', phase='Компания готова.', company_result=result)
            except Exception as error:
                traceback.print_exc()
                self._update(jid, status='failed', phase='Не удалось добавить компанию.', error=str(error))
            finally:
                with self.lock:
                    if not any(j['kind'] == 'add-company' and j['status'] == 'running' for j in self.jobs.values()):
                        self.profile_pending.clear()
                self.poll_wake.set()
            return
        if kind not in ('generate', 'tick'):
            try:
                with self.service.lock():
                    return super()._work(jid, kind, payload)
            except Exception as error:
                self._update(jid, status='failed', phase='Система занята другой операцией.', error=str(error))
                return
        try:
            if kind == 'tick':
                self._update(jid, phase='Обновляем общую базу новостей…')
                automatic = payload.get('automatic') is True
                result = self.service.tick(company_ids=None if automatic else [],
                    parse=not automatic, continuous=automatic,
                    should_continue=(lambda: self.auto_poll and not self.stop.is_set() and not self.profile_pending.is_set()) if automatic else None,
                    generate=automatic, progress=lambda values:self._update(jid, **values))
                deferred = result.get('errors', []) + [r for r in result.get('stages', {}).get('preparation', {}).get('rejected', []) if r.get('retry')]
                paused=result.get('stages',{}).get('preparation',{}).get('pause')
                if paused:deferred.append(paused)
                self.next_process_delay = 60 if deferred else 1 if result.get('continue_soon') else 5
                self._update(jid, status='failed' if deferred else 'complete',
                    phase='Часть обработки отложена; повторим через минуту.' if deferred else 'Новости обновлены.',
                    error='; '.join(r.get('reason', 'Ошибка обработки') for r in deferred) if deferred else None,
                    pipeline_result=result)
                if paused and paused['error_kind']=='access':
                    self._update(jid,phase='Разметка приостановлена: проверьте ключ, доступ к модели и бюджет API.')
                self.poll_wake.set()
            else:
                self._update(jid, phase='Подбираем материалы и вызываем LLM для оценки, отчёта и проверки…')
                item = self.service.generate(payload.get('company_id'), payload.get('as_of'), payload.get('top_news'),feed=True)
                values = dict(status='complete', phase='Отчёт готов.' if item['status'] == 'completed' else 'Новых подходящих новостей нет.',
                    workflow_status=item['status'], workflow=item.get('workflow', {}),
                    report=item.get('report'), saved=item.get('saved'))
                if item['status']=='skipped':
                    values.update(phase='Отчёт отложен после ошибки проверки; остальные новости продолжат обрабатываться.',error=item.get('error'))
                if item['status'] == 'completed':
                    values.update(completion=item['completion'], result=item['result'])
                self._update(jid, **values)
        except Exception as error:
            traceback.print_exc()
            self._update(jid, status='failed', phase='Не удалось завершить операцию; партия сохранена для повтора.', error=str(error))
            self.next_process_delay = 60

    def collect_news(self):
        """Source I/O has its own OS-locked DB and never waits for a report LLM."""
        interval = max(10, self.service.config['pipeline']['interval_seconds'])
        while not self.stop.is_set():
            self.collect_wake.clear()
            if self.auto_poll:
                try:
                    self.collection = dict(status='running', phase='Проверяем источники новостей')
                    result = self.service._parse()
                    self.collection = dict(status='complete', phase='Источники проверены', result=result)
                    self.poll_wake.set()
                except Exception as error:
                    self.collection = dict(status='failed', phase='Повторим проверку источников', error=str(error))
            self.collect_wake.wait(interval)

    def poll(self):
        while not self.stop.is_set():
            self.poll_wake.clear()
            if self.auto_poll:
                try:
                    self.start('tick', {'automatic': True})
                except BlockingIOError:
                    pass
            # Wait for completion, then drain backlog without another collection
            # interval. Avoid waking every second while a paid call is in flight.
            while not self.stop.is_set():
                with self.lock:
                    running = any(j['status'] == 'running' for j in self.jobs.values())
                if not running:
                    break
                self.poll_wake.wait(1)
                self.poll_wake.clear()
            self.poll_wake.wait(self.next_process_delay if self.auto_poll else 60)


class Handler(SearchHandler):
    static_dir = Path(__file__).with_name('web_static')
    public_videos = {'/assets/alfa-animation.mp4': 'alfa-animation.mp4',
                     '/assets/consumption-animation.mp4': 'consumption-animation.mp4'}

    def do_HEAD(self):
        name = self.public_videos.get(urlsplit(self.path).path)
        if name:
            from .media import send_video
            return send_video(self, self.static_dir / 'assets' / name, head=True)
        self.send_response(405)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_header(self, keyword, value):
        if keyword.lower() == 'content-security-policy':
            value = ("default-src 'self'; script-src 'self'; "
                     "style-src 'self' https://fonts.googleapis.com; "
                     "font-src 'self' https://fonts.gstatic.com; "
                     "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return super().send_header(keyword, value)

    def do_GET(self):
        route = urlsplit(self.path)
        video = self.public_videos.get(route.path)
        if video:
            from .media import send_video
            return send_video(self, self.static_dir / 'assets' / video)
        if route.path == '/api/site-events':
            return self.site_events(parse_qs(route.query).get('company', [None])[0])
        if route.path == '/api/currency-rates':
            return self.send(200, self.server.app.currency_rates.get())
        assets = {'/site-adapter.js': ('site-adapter.js', 'text/javascript'),
                  '/assets/empty-news-v1.webp': ('assets/empty-news-v1.webp', 'image/webp'),
                  '/favicon.ico': ('favicon.ico', 'image/x-icon'),
                  '/currency-widget.js': ('currency-widget.js', 'text/javascript'),
                  '/story-cards.js': ('story-cards.js', 'text/javascript'),
                  '/site-panels.js': ('site-panels.js', 'text/javascript'),
                  '/integration.css': ('integration.css', 'text/css')}
        assets.update({'/manage/' + name: ('manage/' + name, mime) for name, mime in (
            ('app.js', 'text/javascript'), ('style.css', 'text/css'),
            ('report-view.js', 'text/javascript'), ('report-view.css', 'text/css'))})
        # Keep old asset addresses available to tabs opened before the redesign.
        assets.update({'/' + name: ('manage/' + name, mime) for name, mime in (
            ('report-view.js', 'text/javascript'), ('report-view.css', 'text/css'))})
        if route.path == '/api/site-feed':
            from .site import feed
            from .topic_quota import hours as topic_quota_hours
            from news_analytics.common import require
            try:
                company_id = parse_qs(route.query).get('company', [None])[0]
                require(company_id is not None, 'Выберите компанию')
                return self.send(200, feed(self.server.app.service.db, company_id,
                    self.server.app.service.config.get('retrieval', {}).get('news_days', 7),
                    admission_policy=policy(self.server.app.service.config),
                    topic_quota_hours=topic_quota_hours(self.server.app.service.config)))
            except (ValueError, OSError) as error:
                return self.send(400, dict(error=str(error)))
        if route.path in ('/manage', '/manage/'):
            return self.send(200, (self.static_dir / 'manage/index.html').read_bytes(), 'text/html; charset=utf-8')
        asset = assets.get(route.path)
        if asset:
            name, content_type = asset
            return self.send(200, (self.static_dir / name).read_bytes(),
                             content_type + '; charset=utf-8' if content_type.startswith('text/') else content_type)
        return super().do_GET()

    def site_events(self, company_id):
        """Same-origin SSE: cheap DB revision checks, no model work on reads."""
        from .site import feed_revision
        from news_analytics.common import dumps
        try:
            revision = feed_revision(self.server.app.service.db, company_id)
        except ValueError as error:
            return self.send(400, dict(error=str(error)))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        previous, last_ping = None, 0
        try:
            while not self.server.app.stop.is_set():
                revision = feed_revision(self.server.app.service.db, company_id)
                if revision != previous:
                    self.wfile.write(('event: feed\ndata: ' + dumps(dict(revision=revision)) + '\n\n').encode('utf-8'))
                    previous = revision
                    last_ping = time.monotonic()
                elif time.monotonic() - last_ping >= 15:
                    self.wfile.write(b': keepalive\n\n')
                    last_ping = time.monotonic()
                self.wfile.flush()
                self.server.app.stop.wait(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.close_connection = True


def serve(config_path, config, host='127.0.0.1', port=8765, browser=True,browser_path='/'):
    app = Application(config_path, config)
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app
    if app.auto_poll:
        from .settings import llm_ready
        if not llm_ready():
            raise ValueError('Для PIPELINE_AUTO_POLL=1 настройте доступ к LLM')
    worker = threading.Thread(target=app.poll, daemon=True)
    worker.start()
    collector = threading.Thread(target=app.collect_news, daemon=True)
    collector.start()
    url = f'http://127.0.0.1:{server.server_port}'+browser_path
    print('Интерфейс: ' + url, flush=True)
    if browser:
        try:
            webbrowser.open(url)
        except Exception:
            print('Откройте адрес в браузере вручную: ' + url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        app.poll_wake.set()
        app.collect_wake.set()
        if worker:
            worker.join(timeout=2)
        server.server_close()
        app.pool.shutdown(wait=True)
