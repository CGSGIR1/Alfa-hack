"""Bank direction, segment isolation, request policy and outage behaviour; no paid calls."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.integration.currency import CurrencyRates, parse_quotes, fetch_quotes, SOURCE, ENDPOINT, CERTIFICATE, MAX_AGE

NOW = datetime(2026,10,25,13,0,tzinfo=timezone.utc).timestamp()
RAW = json.loads((Path(__file__).parent/'fixtures/currency/alfa-cash.json').read_text(encoding='utf-8'))


class CurrencyTests(unittest.TestCase):
    def test_actual_bank_payload_uses_client_direction_and_keeps_quote_date(self):
        rows = parse_quotes(RAW,NOW)
        self.assertEqual([r['code'] for r in rows],['CNY','EUR','USD'])
        usd = rows[-1]
        self.assertEqual((usd['sell_to_bank'],usd['buy_from_bank']),('80.5','82.7'))
        self.assertEqual(usd['rate_at'],'2026-10-23T16:14:00Z')

    def test_coefficient_and_segment_are_not_mixed(self):
        raw=deepcopy(RAW);client=raw['data'][0]['rateByClientType'][0]
        raw['data'][0]['rateByClientType'].insert(0,dict(clientType='corp',ratesByType=[]))
        quote=client['ratesByType'][0]['lastActualRate']
        quote['buy']['valueWithCoefficient']=90.25
        client['ratesByType'].insert(0,dict(rateType='makeCash',lastActualRate={}))
        self.assertEqual(parse_quotes(raw,NOW)[1]['sell_to_bank'],'90.25')

    def test_invalid_future_inverted_or_missing_quotes_are_rejected(self):
        for value in (None,0,-2,'NaN','Infinity',True):
            raw=deepcopy(RAW);raw['data'][0]['rateByClientType'][0]['ratesByType'][0]['lastActualRate']['buy']={'originalValue':value}
            with self.subTest(value=value),self.assertRaises(ValueError):parse_quotes(raw,NOW)
        for change in ({'date':'2030-01-01T00:00:00Z'},{'date':'2026-10-23'}, {'buy':{'originalValue':100}}):
            raw=deepcopy(RAW);raw['data'][0]['rateByClientType'][0]['ratesByType'][0]['lastActualRate'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):parse_quotes(raw,NOW)
        with self.assertRaises(ValueError):parse_quotes({'data':[]},NOW)

    def test_bank_only_tls_and_bounded_timeout(self):
        response=Mock(status_code=200);response.json.return_value=RAW
        session=Mock();session.get.return_value=response
        with patch('src.integration.currency.requests.Session') as factory:
            factory.return_value.__enter__.return_value=session
            fetch_quotes(NOW)
        args,kwargs=session.get.call_args
        self.assertEqual(args,(ENDPOINT,));self.assertEqual(kwargs['verify'],str(CERTIFICATE))
        self.assertFalse(kwargs['allow_redirects']);self.assertEqual(kwargs['timeout'],(3,7))
        self.assertEqual(kwargs['params']['rateType.in'],'rateCass')
        response.status_code=302
        response.url=ENDPOINT;response.headers={'Location':'https://other.example/rates'}
        with patch('src.integration.currency.requests.Session') as factory:
            factory.return_value.__enter__.return_value=session
            with self.assertRaises(ValueError):fetch_quotes(NOW)
        response.headers={'Location':ENDPOINT};session.get.reset_mock()
        with patch('src.integration.currency.requests.Session') as factory:
            factory.return_value.__enter__.return_value=session
            with self.assertRaises(ValueError):fetch_quotes(NOW)
        self.assertEqual(session.get.call_count,3)

    def wait_idle(self,cache):
        # Event.wait yields to the worker without waiting on a real network.
        for _ in range(100):
            with cache.lock:
                if not cache.refreshing:return
            threading.Event().wait(.01)
        self.fail('Currency worker did not finish')

    def test_cache_shared_nonblocking_refresh_failure_expiry_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=[NOW];started=threading.Event();release=threading.Event();calls=[]
            def fetcher():
                calls.append(1);started.set();release.wait(3);return parse_quotes(RAW,clock[0])
            path=Path(tmp)/'rates.json';cache=CurrencyRates(path,fetcher,lambda:clock[0])
            self.assertEqual(cache.get()['status'],'loading');self.assertTrue(started.wait(1))
            for _ in range(5):self.assertEqual(cache.get()['rates'],[])
            self.assertEqual(len(calls),1)
            release.set();self.wait_idle(cache)
            data=cache.get();self.assertEqual(data['status'],'ok');self.assertEqual(len(data['rates']),3)
            data['rates'].clear();self.assertEqual(len(cache.get()['rates']),3)
            self.assertEqual(CurrencyRates(path,clock=lambda:clock[0]).get()['rates'],cache.get()['rates'])
            clock[0]+=61;cache.fetcher=Mock(side_effect=ValueError('bad source'))
            self.assertEqual(cache.get()['status'],'stale');self.wait_idle(cache)
            self.assertEqual(cache.get()['status'],'stale');self.assertEqual(cache.fetcher.call_count,1)
            for _ in range(3):cache.get()
            self.assertEqual(cache.fetcher.call_count,1)
            clock[0]+=MAX_AGE
            self.assertEqual(cache.get()['rates'],[]);self.wait_idle(cache)
            self.assertEqual(cache.get()['status'],'unavailable')

    def test_initial_outage_has_no_fabricated_prices_and_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rates.json';path.write_text('{broken')
            cache=CurrencyRates(path,Mock(side_effect=ValueError('outage')),lambda:NOW)
            cache.get();self.wait_idle(cache)
            result=cache.get();self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['rates'],[]);self.assertEqual(result['source_url'],SOURCE)


if __name__=='__main__':unittest.main()
