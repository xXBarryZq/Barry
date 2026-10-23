import json
from pathlib import Path
import sys
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/windows'))
from wind_data import CHINA, MINUTE, bars_from_result, merge_bars, quote_from_result, source_time, timestamp
from wind_dashboard import WindDashboard, make_server


def result(fields, data, codes=None, times=None, error=0):
    return SimpleNamespace(Fields=fields, Data=data, Codes=codes or ['510300.SH'], Times=times or [], ErrorCode=error)


class WindDataTest(unittest.TestCase):
    def setUp(self):
        self.query = datetime(2026, 10, 23, 16, 14, 25)
        self.quotes = result(['RT_LAST', 'RT_PRE_CLOSE', 'RT_VOL', 'RT_TIME'], [[4.2], [4.1], [10000.0], [153011.0]], times=[self.query])
        self.dates = result(['RT_DATE'], [[20261023.0]])

    def test_source_clock_is_hhmmss_and_query_time_does_not_refresh_quote(self):
        quote, issue = quote_from_result(self.quotes, self.dates, timestamp(self.query))
        self.assertEqual(issue, '')
        self.assertEqual(quote['timestamp'], timestamp(datetime(2026, 10, 23, 15, 30, 11)))
        self.assertEqual(quote['receivedAt'], timestamp(self.query))
        self.assertGreater(quote['receivedAt'] - quote['timestamp'], 90_000)
        self.assertEqual(source_time(93005).isoformat(), '09:30:05')

    def test_missing_source_date_does_not_borrow_request_date(self):
        quote, issue = quote_from_result(self.quotes, None, timestamp(self.query))
        self.assertEqual(quote['price'], 4.2)
        self.assertIsNone(quote['timestamp'])
        self.assertIn('日期或时间未确认', issue)
        self.assertIn('15:30:11', quote['timeLabel'])

    def test_future_source_date_is_rejected(self):
        dates = result(['RT_DATE'], [[20261026]])
        quote, issue = quote_from_result(self.quotes, dates, timestamp(self.query))
        self.assertIsNone(quote['timestamp'])
        self.assertTrue(issue)

    def test_nan_minutes_are_omitted_without_filling_or_collapsing_time(self):
        times = [datetime(2026, 10, 23, 14, 57) + timedelta(minutes=i) for i in range(4)]
        close = [4.177, float('nan'), float('nan'), 4.178]
        data = result(['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'], [[4.17] * 4, [4.18] * 4, [4.16] * 4, close, [100] * 4], times=times)
        bars, seen, missing = bars_from_result(data, timestamp(self.query))
        self.assertEqual(len(bars), 2)
        self.assertEqual(missing, 2)
        self.assertEqual(len(seen), 4)
        self.assertEqual(bars[-1]['time'] - bars[0]['time'], 3 * MINUTE)
        self.assertEqual(bars[-1]['time'], timestamp(datetime(2026, 10, 23, 14, 59)))

    def test_missing_correction_removes_cached_price_and_unfinished_bar_is_ignored(self):
        end = datetime(2026, 10, 23, 15, 0)
        data = result(['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'], [[4.1, 4.1], [4.2, 4.2], [4.0, 4.0], [float('nan'), 4.15], [10, 10]], times=[end, end + timedelta(minutes=1)])
        bars, seen, missing = bars_from_result(data, timestamp(end) + 30_000)
        old = [{'time': timestamp(end) - MINUTE, 'close': 4.1}]
        self.assertEqual(bars, [])
        self.assertEqual(merge_bars(old, bars, seen), [])
        self.assertEqual(missing, 1)

    def test_multi_symbol_quotes_map_by_codes_and_preserve_missing_date(self):
        board = WindDashboard(symbols=('SH.510300', 'SH.600519'))
        class API:
            def wsq(self, codes, fields):
                if fields == 'rt_date':
                    return result([], [], error=-1)
                return result(['RT_LAST', 'RT_PRE_CLOSE', 'RT_VOL', 'RT_TIME'], [[1335, 4.178], [1300, 4.107], [50, 100], [153011, 153011]], codes=['600519.SH', '510300.SH'])
        self.assertIs(board.refresh_quotes(API(), None), False)
        self.assertEqual(board.states['SH.510300']['quote']['price'], 4.178)
        self.assertEqual(board.states['SH.600519']['quote']['price'], 1335)
        self.assertIsNone(board.states['SH.510300']['quote']['timestamp'])


class LocalServerTest(unittest.TestCase):
    def setUp(self):
        self.dashboard = WindDashboard()
        self.server = make_server(self.dashboard, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.dashboard.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_local_pages_and_health_do_not_fabricate_quotes(self):
        with urlopen(self.base + '/api/snapshot', timeout=3) as response:
            data = json.load(response)
        self.assertEqual(data['provider'], 'wind-local')
        self.assertTrue(data['computeStrategies'])
        self.assertTrue(all(item['quote'] is None for item in data['instruments']))
        for asset in ('/', '/app.js', '/strategy.js', '/style.css'):
            with urlopen(self.base + asset, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def test_local_server_refuses_foreign_hosts_and_private_files(self):
        request = Request(self.base + '/api/snapshot', headers={'Host': 'example.com'})
        with self.assertRaises(HTTPError) as denied:
            urlopen(request, timeout=3)
        self.assertEqual(denied.exception.code, 403)
        for path in ('/.env', '/tools/windows/wind_dashboard.py', '/api/ingest'):
            with self.assertRaises(HTTPError) as missing:
                urlopen(self.base + path, timeout=3)
            self.assertEqual(missing.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
