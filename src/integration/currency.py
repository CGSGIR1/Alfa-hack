"""Public Alfa-Bank cash quotes, independent of news, companies and paid LLMs."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import threading
import time

import requests

SOURCE = 'https://alfabank.ru/currency/'
ENDPOINT = 'https://alfabank.ru/api/v1/scrooge/currencies/alfa-rates'
CERTIFICATE = Path(__file__).with_name('certs') / 'russian_trusted_root_ca.pem'
PERIOD = 60
MAX_AGE = 24 * 60 * 60
ORDER = ('CNY', 'EUR', 'USD')
LOG = logging.getLogger(__name__)


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Quote timestamp must include timezone')
    return result.timestamp()


def amount(value):
    if not isinstance(value, dict):
        raise ValueError('Missing quote')
    raw = value.get('valueWithCoefficient')
    if raw is None:
        raw = value.get('originalValue')
    try:
        number = Decimal(str(raw))
    except InvalidOperation as error:
        raise ValueError('Invalid quote') from error
    if not number.is_finite() or number <= 0 or number > 1000000:
        raise ValueError('Invalid quote')
    return format(number, 'f')


def parse_quotes(payload, now):
    """Select the exact segment/type; do not mix card, online and cash rates."""
    result = {}
    for currency in payload.get('data', []):
        code = currency.get('currencyCode')
        if code not in ORDER:
            continue
        for client in currency.get('rateByClientType', []):
            if client.get('clientType') != 'standardCC':
                continue
            for group in client.get('ratesByType', []):
                if group.get('rateType') != 'rateCass':
                    continue
                quote = group.get('lastActualRate') or {}
                at = quote.get('date', '')
                if timestamp(at) > now + 300:
                    raise ValueError('Future quote')
                # API buy/sell are from the bank's perspective. UI is the client's.
                sell, buy = amount(quote.get('buy')), amount(quote.get('sell'))
                if Decimal(sell) > Decimal(buy):
                    raise ValueError('Inverted quote spread')
                row = dict(code=code, sell_to_bank=sell, buy_from_bank=buy, rate_at=at)
                if code not in result or timestamp(at) > timestamp(result[code]['rate_at']):
                    result[code] = row
    if not result:
        raise ValueError('No cash quotes in bank response')
    return [result[code] for code in ORDER if code in result]


def fetch_quotes(now=None):
    now = time.time() if now is None else now
    params = {'date.lte': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='seconds'),
              'currencyCode.in': ','.join(ORDER), 'rateType.in': 'rateCass',
              'lastActualForDate.eq': 'true', 'clientType.eq': 'standardCC', 'segmentType.eq': 'none'}
    # This trust bundle is used only for this fixed bank URL. No system/browser
    # trust is changed; chain, hostname and expiration checks remain enabled.
    with requests.Session() as session:
        for _ in range(3):
            response = session.get(ENDPOINT, params=params, timeout=(3, 7), allow_redirects=False,
                verify=str(CERTIFICATE), headers={'Accept': 'application/json', 'Referer': SOURCE,
                'Origin': 'https://alfabank.ru',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'})
            response.raise_for_status()
            if response.status_code == 200:
                return parse_quotes(response.json(), now)
            # Bank may set routing cookies and redirect to the exact same URL.
            # A redirect cannot change the host, path, parameters or TLS scope.
            if response.status_code not in (302, 307) or response.headers.get('Location') != response.url:
                raise ValueError('Unexpected bank response')
        raise ValueError('Bank redirect loop')


class CurrencyRates:
    """One shared cache; slow bank requests never block the page or report queue."""
    def __init__(self, cache_file, fetcher=fetch_quotes, clock=time.time):
        self.path = Path(cache_file)
        self.fetcher, self.clock = fetcher, clock
        self.lock = threading.Lock()
        self.snapshot = None
        self.refreshing = False
        self.failed = False
        self.next_attempt = 0
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved.get('version') == 1 and saved.get('source_url') == SOURCE:
                # Cache is accepted only if it has the same validated representation.
                rows = saved['rates']
                if not rows or len({r['code'] for r in rows}) != len(rows):
                    raise ValueError('Invalid cached quotes')
                for row in rows:
                    if row['code'] not in ORDER or timestamp(row['rate_at']) > self.clock() + 300:
                        raise ValueError('Invalid cached quote')
                    if Decimal(amount({'originalValue':row['sell_to_bank']})) > Decimal(amount({'originalValue':row['buy_from_bank']})):
                        raise ValueError('Inverted cached quote')
                fetched = timestamp(saved['fetched_at'])
                if fetched <= self.clock() + 300:
                    self.snapshot = saved
                    self.next_attempt = min(fetched + PERIOD, self.clock() + PERIOD)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def get(self):
        now = self.clock()
        with self.lock:
            if not self.refreshing and now >= self.next_attempt:
                self.refreshing = True
                self.next_attempt = now + PERIOD
                threading.Thread(target=self._refresh, daemon=True, name='alfa-currency').start()
            saved = self.snapshot
            usable = saved and 0 <= now - timestamp(saved['fetched_at']) <= MAX_AGE
            stale = bool(usable and (self.failed or now - timestamp(saved['fetched_at']) >= PERIOD))
            return dict(source='Альфа-Банк', source_url=SOURCE, quote_type='cash',
                quote_label='Наличные · USD новых серий',
                status='stale' if stale else 'ok' if usable else 'loading' if self.refreshing else 'unavailable',
                refreshing=self.refreshing, fetched_at=saved['fetched_at'] if usable else None,
                rates=deepcopy(saved['rates']) if usable else [], refresh_seconds=PERIOD)

    def _refresh(self):
        try:
            rows = self.fetcher()
            saved = dict(version=1, source_url=SOURCE, rates=rows,
                fetched_at=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec='seconds'))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
            with self.lock:
                self.snapshot, self.failed = saved, False
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            LOG.warning('Alfa currency refresh failed: %s', type(error).__name__)
            with self.lock:
                self.failed = True
        finally:
            with self.lock:
                self.refreshing = False
                self.next_attempt = self.clock() + PERIOD
