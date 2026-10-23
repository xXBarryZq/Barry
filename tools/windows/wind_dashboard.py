"""Local-only Wind dashboard. Python standard library + terminal-provided WindPy."""

import argparse
import copy
import importlib
import json
import mimetypes
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from wind_data import CHINA, WIND_CODES, bars_from_result, merge_bars, quote_from_result

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / 'src' / 'instruments.json').read_text(encoding='utf-8'))
DEFAULT_SYMBOLS = ('SH.510300', 'SH.600519', 'SHFE.rb')


class WindDashboard:
    def __init__(self, symbols=DEFAULT_SYMBOLS, poll_seconds=5):
        if not symbols or any(symbol not in WIND_CODES for symbol in symbols):
            raise ValueError('品种必须来自预置 Wind 代码表')
        self.symbols = tuple(dict.fromkeys(symbols))
        self.poll_seconds = max(3, poll_seconds)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.connected = False
        self.message = '正在连接本机 Wind……'
        self.states = {item['id']: {
            'quote': None, 'bars': [], 'quoteIssue': '', 'historyIssue': '', 'missingBars': 0,
        } for item in CATALOG}

    def set_status(self, connected, message):
        with self.lock:
            self.connected, self.message = connected, message

    def snapshot(self):
        now = int(time.time() * 1000)
        with self.lock:
            states = copy.deepcopy(self.states)
            connected, message = self.connected, self.message
        items = []
        for original in CATALOG:
            item = {key: value for key, value in original.items() if key != 'base'}
            state = states[item['id']]
            if item['market'] == 'us':
                issue = '美股行情待接入富途；此处不生成替代价格。'
            elif item['id'] not in self.symbols:
                issue = '此品种尚未订阅 Wind 行情。'
            elif not connected:
                issue = message
            else:
                issue = state['quoteIssue'] or state['historyIssue']
            item.update(quote=state['quote'], bars=state['bars'], dataIssue=issue, missingBars=state['missingBars'])
            items.append(item)
        return {'mode': 'live', 'provider': 'wind-local', 'computeStrategies': True,
                'source': 'Wind 本机接口 · 5 秒轮询 · 非逐笔推送', 'serverTime': now,
                'status': {'connected': connected, 'message': message}, 'instruments': items}

    @staticmethod
    def index(result, code):
        codes = [str(value).upper() for value in (getattr(result, 'Codes', None) or [])]
        return codes.index(code.upper())

    def refresh_quotes(self, api, date_supported):
        codes = ','.join(WIND_CODES[symbol] for symbol in self.symbols)
        quotes = api.wsq(codes, 'rt_last,rt_pre_close,rt_vol,rt_time')
        received = int(time.time() * 1000)
        if getattr(quotes, 'ErrorCode', None) != 0:
            with self.lock:
                for symbol in self.symbols:
                    self.states[symbol]['quoteIssue'] = 'Wind 报价请求失败，错误码 ' + str(getattr(quotes, 'ErrorCode', '未知'))
            return date_supported
        dates = None
        if date_supported is not False:
            dates = api.wsq(codes, 'rt_date')
            date_supported = getattr(dates, 'ErrorCode', None) == 0
        for symbol in self.symbols:
            code = WIND_CODES[symbol]
            try:
                index = self.index(quotes, code)
                date_index = self.index(dates, code) if date_supported else 0
                quote, issue = quote_from_result(quotes, dates, received, index, date_index)
            except (ValueError, TypeError, IndexError):
                with self.lock:
                    self.states[symbol]['quoteIssue'] = 'Wind 返回的该品种报价字段缺失；请核对数据权限或代码。'
                continue
            with self.lock:
                state = self.states[symbol]
                # Source-time regressions must not replace a later known quote.
                previous = state['quote']
                if previous and previous['timestamp'] is not None and quote['timestamp'] is not None and quote['timestamp'] < previous['timestamp']:
                    state['quoteIssue'] = 'Wind 源行情时间倒退，已保留上次报价并暂停策略。'
                    continue
                state['quote'], state['quoteIssue'] = quote, issue
        return date_supported

    def refresh_history(self, api, symbol):
        end = datetime.now(CHINA).replace(tzinfo=None)
        with self.lock:
            existing = copy.deepcopy(self.states[symbol]['bars'])
        # Bootstrap only once; then overlap a few minutes to capture corrections.
        if existing:
            begin = datetime.fromtimestamp(existing[-1]['time'] / 1000, CHINA).replace(tzinfo=None) - timedelta(minutes=5)
        else:
            begin = end - timedelta(days=3)
        result = api.wsi(WIND_CODES[symbol], 'open,high,low,close,volume', begin, end, 'BarSize=1')
        try:
            bars, seen, missing = bars_from_result(result, int(time.time() * 1000))
        except (ValueError, TypeError, IndexError):
            with self.lock:
                self.states[symbol]['historyIssue'] = 'Wind 分钟 OHLCV 查询失败，错误码 ' + str(getattr(result, 'ErrorCode', '未知')) + '；请核对分钟权限。'
            return False
        with self.lock:
            state = self.states[symbol]
            state['bars'] = merge_bars(existing, bars, seen)
            state['missingBars'] = missing
            state['historyIssue'] = ''
        return True

    def run(self):
        api = None
        try:
            api = importlib.import_module('WindPy').w
            result = api.start()
            if getattr(result, 'ErrorCode', None) != 0 or not api.isconnected():
                self.set_status(False, 'Wind 连接失败。请登录本机 Wind，并使用同一个 Python 重新启动。')
                return
            self.set_status(True, '本机 Wind 已连接；报价日期未核实或分钟数据不足的品种会暂停策略。')
            print('Wind 已连接，正在读取预置品种的行情和分钟数据。', flush=True)
            due = {symbol: 0 for symbol in self.symbols}
            date_supported = None
            while not self.stop.is_set():
                if not api.isconnected():
                    self.set_status(False, 'Wind 连接已断开。请重新登录 Wind 后重启本工具。')
                    return
                date_supported = self.refresh_quotes(api, date_supported)
                for symbol in self.symbols:
                    if self.stop.is_set():
                        break
                    if time.monotonic() >= due[symbol]:
                        success = self.refresh_history(api, symbol)
                        due[symbol] = time.monotonic() + (60 if success else 300)
                self.stop.wait(self.poll_seconds)
        except ImportError:
            self.set_status(False, '当前 Python 找不到 WindPy，请使用已验证连接成功的 Python 启动本工具。')
        except Exception as error:
            # Do not print vendor exception text: it may contain local account details.
            self.set_status(False, 'Wind 接口异常（' + type(error).__name__ + '），请查看客户端连接状态后重启。')
        finally:
            if api is not None:
                try:
                    api.stop()
                except Exception:
                    pass


FILES = {'/': ROOT / 'public/index.html', '/app.js': ROOT / 'public/app.js',
         '/style.css': ROOT / 'public/style.css', '/favicon.svg': ROOT / 'public/favicon.svg',
         '/strategy.js': ROOT / 'src/strategy.js'}


def make_server(dashboard, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def write_headers(self, status, kind, length=None):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if length is not None:
                self.send_header('Content-Length', str(length))
            self.end_headers()

        def do_GET(self):
            # Restrict DNS rebinding and accidental access through a foreign Host.
            if self.headers.get('Host') not in {'127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)}:
                self.write_headers(403, 'text/plain; charset=utf-8', 0)
                return
            path = urlsplit(self.path).path
            if path == '/api/events':
                self.connection.settimeout(15)
                self.write_headers(200, 'text/event-stream; charset=utf-8')
                try:
                    self.wfile.write(b'retry: 3000\n')
                    while not dashboard.stop.is_set():
                        data = json.dumps(dashboard.snapshot(), ensure_ascii=False, allow_nan=False)
                        self.wfile.write(('data: ' + data + '\n\n').encode('utf-8'))
                        self.wfile.flush()
                        if dashboard.stop.wait(3):
                            break
                except (OSError, ValueError):
                    pass
                self.close_connection = True
                return
            if path in ('/api/health', '/api/snapshot'):
                snapshot = dashboard.snapshot()
                data = snapshot if path.endswith('snapshot') else {
                    'status': 'ok', 'mode': 'live', 'provider': 'wind-local',
                    'providerConnected': snapshot['status']['connected'],
                    'instruments': len(snapshot['instruments']),
                    'quotes': sum(item['quote'] is not None for item in snapshot['instruments']),
                    'timestampedQuotes': sum(item['quote'] is not None and item['quote']['timestamp'] is not None for item in snapshot['instruments']),
                }
                body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
                self.write_headers(200, 'application/json; charset=utf-8', len(body))
                self.wfile.write(body)
                return
            if path in FILES:
                body = FILES[path].read_bytes()
                kind = 'text/javascript' if path.endswith('.js') else mimetypes.guess_type(str(FILES[path]))[0]
                self.write_headers(200, (kind or 'application/octet-stream') + '; charset=utf-8', len(body))
                self.wfile.write(body)
                return
            self.write_headers(404, 'text/plain', 0)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main():
    if sys.platform != 'win32':
        print('请在已安装并登录 Wind 的 Windows 电脑运行；云环境不含本机 Wind。')
        return 2
    parser = argparse.ArgumentParser(description='Barry 本机 Wind 行情看板')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--symbols', default=','.join(DEFAULT_SYMBOLS), help='使用 src/instruments.json 中的 id，以英文逗号分隔')
    args = parser.parse_args()
    if args.port < 1 or args.port > 65535:
        parser.error('端口必须为 1–65535')
    dashboard = WindDashboard(tuple(value.strip() for value in args.symbols.split(',')))
    try:
        server = make_server(dashboard, args.port)
    except OSError:
        print('端口不可用。请检查是否已打开本工具，或使用 --port 更换端口。')
        return 3
    # Keep all WindPy/native SDK operations on the main thread.
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
    worker.start()
    print('Barry 本机行情看板已启动，端口 ' + str(args.port) + '。请保持此窗口和 Wind 客户端打开。', flush=True)
    print('按 Ctrl+C 停止。美股数据暂待接入富途；不会生成模拟替代价格。', flush=True)
    if not args.no_browser:
        webbrowser.open('http://127.0.0.1:' + str(args.port))
    try:
        dashboard.run()
        # Keep the explanatory error page open when a provider prerequisite fails.
        while not dashboard.stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
