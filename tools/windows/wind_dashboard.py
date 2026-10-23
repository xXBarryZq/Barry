"""Local-only Wind dashboard. Python standard library + terminal-provided WindPy."""

import argparse
import os
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from watchlist import WindDashboard, DEFAULT_SYMBOLS

ROOT = Path(__file__).resolve().parents[2]


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

        def json(self, status, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.write_headers(status, 'application/json; charset=utf-8', len(body))
            self.wfile.write(body)

        def local_request(self):
            hosts = {'127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)}
            origin = self.headers.get('Origin')
            return self.headers.get('Host') in hosts and (origin is None or origin in {'http://' + host for host in hosts})

        def do_GET(self):
            # Restrict DNS rebinding and accidental access through a foreign Host.
            if not self.local_request():
                self.write_headers(403, 'text/plain; charset=utf-8', 0)
                return
            path = urlsplit(self.path).path
            if path == '/api/search':
                query = parse_qs(urlsplit(self.path).query).get('q',[''])[0]
                self.json(200, dashboard.search(query))
                return
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

        def do_POST(self):
            self.connection.settimeout(15)
            if not self.local_request():
                self.json(403, {'error':'仅允许本机页面修改自选。'})
                self.close_connection = True
                return
            if not self.headers.get('Content-Type','').startswith('application/json'):
                self.json(415, {'error':'仅接受 JSON 请求。'})
                self.close_connection = True
                return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if length < 1 or length > 2_000_000:
                    self.json(413, {'error':'请求大小无效。'})
                    self.close_connection = True
                    return
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(data,dict):
                    raise ValueError('请求必须为对象')
                path = urlsplit(self.path).path
                if path == '/api/watchlist':
                    value = data.get('code')
                    if not isinstance(value,str) or len(value)>80:
                        raise ValueError('请输入证券代码')
                    item = dashboard.add_watch(value)
                    self.json(200, {'instrument':item})
                elif path == '/api/watchlist/remove':
                    value = data.get('id')
                    if not isinstance(value,str):
                        raise ValueError('品种 id 无效')
                    dashboard.remove_watch(value)
                    self.json(200, {'removed':value})
                elif path == '/api/settings':
                    self.json(200, {'settings':dashboard.set_zones(data)})
                elif path == '/api/catalog':
                    text = data.get('csv')
                    if not isinstance(text,str):
                        raise ValueError('目录内容无效')
                    self.json(200, {'imported':dashboard.import_directory(text)})
                else:
                    self.json(404, {'error':'接口不存在'})
            except ValueError as error:
                self.json(400, {'error':str(error)})
            except (TypeError,KeyError):
                self.json(400, {'error':'请求字段无效。'})
            except OSError:
                self.json(500, {'error':'本机自选配置无法保存，请检查文件夹权限。'})

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
    parser.add_argument('--state-file', help='覆盖本机自选配置路径')
    args = parser.parse_args()
    if args.port < 1 or args.port > 65535:
        parser.error('端口必须为 1–65535')
    state_file = args.state_file or str(Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local')) / 'BarryMarketWatch/settings.json')
    try:
        dashboard = WindDashboard(tuple(value.strip() for value in args.symbols.split(',')), state_path=state_file)
    except (ValueError,KeyError,OSError):
        print('本机自选配置无法读取，请检查配置文件；程序不会覆盖原文件。')
        return 4
    try:
        server = make_server(dashboard, args.port)
    except OSError:
        print('端口不可用。请检查是否已打开本工具，或使用 --port 更换端口。')
        return 3
    # Keep all WindPy/native SDK operations on the main thread.
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
    worker.start()
    print('Barry 本机行情看板已启动，端口 ' + str(args.port) + '。请保持此窗口和 Wind 客户端打开。', flush=True)
    print('按 Ctrl+C 停止。美股使用 Wind；时区确认前显示价格但暂停买卖标记。', flush=True)
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
