"""Wind-backed editable watchlist, persisted locally without prices or credentials."""

import copy
import importlib
import queue
import threading
import time
from datetime import datetime, timedelta

from swing_data import fetch_history
from research import ResearchStore
from security_catalog import SecurityCatalog, SettingsStore, candidates, instrument
from wind_data import CHINA, bars_from_result, cells, get_zone, merge_bars, quote_from_result

DEFAULT_SYMBOLS = ('SH.510300', 'SH.600519', 'SHFE.rb', 'US.AAPL', 'US.SPY')


class WindDashboard:
    def __init__(self, symbols=DEFAULT_SYMBOLS, poll_seconds=5, state_path=None):
        self.catalog = SecurityCatalog()
        self.store = SettingsStore(state_path)
        self.research = ResearchStore(self.store.path.with_name("research.json") if self.store.path is not None else None)
        self.stop, self.lock = threading.Event(), threading.RLock()
        self.connected, self.message = False, '正在连接本机 Wind……'
        self.poll_seconds = max(3, poll_seconds)
        self.commands, self.pending, self.resolve_errors = queue.Queue(), set(), {}
        self.date_supported, self.date_due, self.history_due = {}, {}, {}
        self.zones = {'usQuoteZone': self.store.data.get('usQuoteZone', 'unknown'),
                      'usBarZone': self.store.data.get('usBarZone', 'unknown')}
        self.catalog.import_rows(self.store.data.get('directory', []))
        saved = self.store.data.get('watchlist')
        if saved is not None:
            symbols = []
            for row in saved:
                item = instrument(row['windCode'], row['name'])
                self.catalog.add(item)
                symbols.append(item['id'])
        if len(symbols) > 50 or any(self.catalog.get(symbol) is None for symbol in symbols):
            raise ValueError('自选列表无效或超过 50 个品种')
        self.symbols = list(dict.fromkeys(symbols))
        self.states = {symbol:self.empty_state() for symbol in self.symbols}

    @staticmethod
    def empty_state():
        return {'quote':None, 'bars':[], 'quoteIssue':'', 'historyIssue':'', 'missingBars':0, 'dailyBars':[], 'hourlyBars':[], 'calendar':[], 'expectedDailyDate':None, 'swingRevision':0, 'swingIssue':'正在补充日线和60分钟历史。'}

    def set_status(self, connected, message):
        with self.lock:
            self.connected, self.message = connected, message

    def snapshot(self):
        with self.lock:
            states, symbols = copy.deepcopy(self.states), list(self.symbols)
            connected, message = self.connected, self.message
            zones = dict(self.zones)
        items = []
        for symbol in symbols:
            item, state = self.catalog.get(symbol), states[symbol]
            issue = state['quoteIssue'] or state['historyIssue'] if connected else message
            item.update(quote=state['quote'], bars=state['bars'], dataIssue=issue, missingBars=state['missingBars'], dailyBars=state['dailyBars'], hourlyBars=state['hourlyBars'], calendar=state['calendar'], expectedDailyDate=state['expectedDailyDate'], swingRevision=state['swingRevision'], swingIssue=state['swingIssue'], research=self.research.snapshot(item['windCode']))
            items.append(item)
        return {'mode':'live', 'provider':'wind-local', 'computeStrategies':True,
                'source':'Wind 本机行情 · 5 秒轮询', 'serverTime':int(time.time()*1000),
                'status':{'connected':connected,'message':message}, 'instruments':items,
                'editableWatchlist':True, 'directoryStatus':self.catalog.a_share_status,
                'settings':zones}

    def persist(self, symbols=None, extra=None):
        ids = self.symbols if symbols is None else symbols
        self.store.save({'watchlist':[self.catalog.get(symbol) for symbol in ids], **(extra or {})})

    def add_watch(self, value):
        item = self.catalog.get(value)
        if item is None:
            raise ValueError('请先搜索并确认 Wind 返回的证券名称，再添加。')
        with self.lock:
            if item['id'] in self.symbols:
                return item
            if len(self.symbols) >= 50:
                raise ValueError('自选最多 50 个品种，请先移除一些。')
            symbols = [*self.symbols, item['id']]
            self.persist(symbols)
            self.states[item['id']] = self.empty_state()
            self.symbols = symbols
            self.history_due[item['id']] = 0
        return item

    def remove_watch(self, value):
        with self.lock:
            symbols = [symbol for symbol in self.symbols if symbol != value]
            self.persist(symbols)
            self.symbols = symbols
            self.states.pop(value, None)
            self.history_due.pop(value, None)

    def set_zones(self, values):
        settings = {key:values.get(key, self.zones[key]) for key in self.zones}
        for name in settings.values():
            get_zone(name)
        with self.lock:
            self.persist(extra=settings)
            self.zones = settings
            for symbol in self.symbols:
                if self.catalog.get(symbol)['market'] == 'us':
                    self.states[symbol] = self.empty_state()
                    self.history_due[symbol] = 0
        return settings

    def import_directory(self, text):
        with self.lock:
            with self.catalog.lock:
                previous = dict(self.catalog.items)
                try:
                    count = self.catalog.import_csv(text)
                    rows = [{'wind_code':item['windCode'], 'sec_name':item['name']} for item in self.catalog.items.values()]
                    self.persist(extra={'directory':rows})
                except Exception:
                    self.catalog.items = previous
                    raise
        return count

    def search(self, text):
        text = text.strip()[:80]
        results = self.catalog.search(text)
        possible = candidates(text)
        with self.lock:
            if possible and len(text) >= 2 and self.catalog.get(text) is None and text not in self.pending and text not in self.resolve_errors:
                if len(self.pending) < 20 and self.connected:
                    self.pending.add(text)
                    self.commands.put(text)
            pending = text in self.pending
            note = self.resolve_errors.get(text, self.catalog.a_share_status)
            active = set(self.symbols)
        return {'results':[{**item, 'added':item['id'] in active} for item in results],
                'pending':pending, 'message':note, 'scope':'Wind A股名称目录、已导入目录及已校验代码'}

    def resolve_commands(self, api):
        for _ in range(2):
            try:
                text = self.commands.get_nowait()
            except queue.Empty:
                break
            found = False
            try:
                for code in candidates(text):
                    result = api.wss(code, 'sec_name')
                    name = cells(result).get('SEC_NAME')
                    if not isinstance(name, str) or name.strip().casefold() in ('','nan','none','null','n/a'):
                        continue
                    self.catalog.add(instrument(code, name))
                    found = True
                    break
            except Exception:
                pass
            with self.lock:
                self.pending.discard(text)
                if not found:
                    self.resolve_errors[text] = 'Wind 未返回有效证券名称；请检查完整代码、交易所后缀或接口权限。'

    def load_name_directory(self, api):
        try:
            result = api.wset('sectorconstituent', 'sectorid=a001010100000000')
            if getattr(result, 'ErrorCode', None) != 0:
                raise ValueError('目录读取失败')
            fields = [str(field).upper() for field in (result.Fields or [])]
            codes, names = result.Data[fields.index('WIND_CODE')], result.Data[fields.index('SEC_NAME')]
            count = self.catalog.import_rows({'wind_code':code,'sec_name':name} for code,name in zip(codes,names))
            self.catalog.a_share_status = f'已加载 {count} 个 Wind A股名称；ETF和美股可用完整代码查询，或导入名称目录。'
        except Exception:
            self.catalog.a_share_status = 'Wind A股名称目录未加载；可按完整代码查询，或导入 Wind 导出的名称目录。'

    @staticmethod
    def index(result, code):
        return [str(value).upper() for value in (getattr(result,'Codes',None) or [])].index(code.upper())

    def refresh_quotes(self, api, unused=None):
        with self.lock:
            instruments = [self.catalog.get(symbol) for symbol in self.symbols]
        groups = {'cn':[item for item in instruments if item['market'] != 'us'],
                  'us':[item for item in instruments if item['market'] == 'us']}
        for group, items in groups.items():
            if not items:
                continue
            try:
                codes = ','.join(item['windCode'] for item in items)
                quotes = api.wsq(codes, 'rt_last,rt_pre_close,rt_vol,rt_time')
                if getattr(quotes,'ErrorCode',None) != 0:
                    raise ValueError('报价请求失败')
                dates = None
                if self.date_supported.get(group) is not False or time.monotonic() >= self.date_due.get(group,0):
                    dates = api.wsq(codes,'rt_date')
                    self.date_supported[group] = getattr(dates,'ErrorCode',None) == 0
                    self.date_due[group] = time.monotonic()+60
                zone = get_zone(self.zones['usQuoteZone']) if group == 'us' else CHINA
            except Exception:
                with self.lock:
                    for item in items:
                        if item['id'] in self.states:
                            self.states[item['id']]['quoteIssue'] = '该市场 Wind 报价读取失败，请核对代码、权限或时区设置。'
                continue
            for item in items:
                try:
                    code = item['windCode']
                    index = self.index(quotes,code)
                    date_index = self.index(dates,code) if self.date_supported[group] and dates is not None else 0
                    quote, issue = quote_from_result(quotes,dates,int(time.time()*1000),index,date_index,zone)
                except (ValueError,TypeError,IndexError):
                    with self.lock:
                        if item['id'] in self.states:
                            self.states[item['id']]['quoteIssue'] = '该品种报价字段缺失，请核对 Wind 权限或代码。'
                    continue
                with self.lock:
                    state = self.states.get(item['id'])
                    if state is None:
                        continue
                    previous = state['quote']
                    if previous and previous['timestamp'] is not None and quote['timestamp'] is not None and quote['timestamp'] < previous['timestamp']:
                        state['quoteIssue'] = '源行情时间倒退，保留上次报价并暂停买卖标记。'
                    else:
                        state['quote'], state['quoteIssue'] = quote, issue
        return dict(self.date_supported)

    def refresh_history(self, api, symbol):
        item = self.catalog.get(symbol)
        try:
            zone = get_zone(self.zones['usBarZone']) if item['market'] == 'us' else CHINA
            if zone is None:
                raise ValueError('Wind 美股分钟时间口径尚未确认；暂不生成买卖标记。')
            end = datetime.now(zone).replace(tzinfo=None)
            with self.lock:
                existing = copy.deepcopy(self.states.get(symbol, self.empty_state())['bars'])
            begin = datetime.fromtimestamp(existing[-1]['time']/1000,zone).replace(tzinfo=None)-timedelta(minutes=5) if existing else end-timedelta(days=3)
            result = api.wsi(item['windCode'],'open,high,low,close,volume',begin,end,'BarSize=1')
            bars, seen, missing = bars_from_result(result,int(time.time()*1000),zone)
        except Exception:
            with self.lock:
                if symbol in self.states:
                    self.states[symbol]['historyIssue'] = 'Wind 美股分钟时区尚未确认。' if item['market']=='us' and self.zones['usBarZone']=='unknown' else '分钟 OHLCV 查询失败，请核对数据权限、代码和时区。'
            return False
        with self.lock:
            if symbol in self.states:
                self.states[symbol].update(bars=merge_bars(existing,bars,seen),missingBars=missing,historyIssue='')
        return True

    def refresh_swing_history(self, api, symbol):
        item = self.catalog.get(symbol)
        try:
            if item['market'] == 'futures':
                raise ValueError('期货波段规则尚未完成夜盘与换月验证。')
            zone = get_zone(self.zones['usBarZone']) if item['market']=='us' else CHINA
            if zone is None:
                raise ValueError('Wind 美股历史时间口径尚未确认。')
            with self.lock:
                previous = copy.deepcopy(self.states.get(symbol, self.empty_state()))
            data = fetch_history(api,item,previous,zone)
            with self.lock:
                if symbol in self.states:
                    self.states[symbol].update(**data,swingIssue='',historyIssue='',swingRevision=previous['swingRevision']+1)
            return True
        except Exception as error:
            message = str(error) if isinstance(error,ValueError) else '日线/60分钟历史读取失败，请核对 WSD、WSI 和交易日历权限。'
            with self.lock:
                if symbol in self.states:
                    self.states[symbol]['swingIssue'] = message
            return False

    def run(self):
        api = None
        try:
            api = importlib.import_module('WindPy').w
            result = api.start()
            if getattr(result,'ErrorCode',None)!=0 or not api.isconnected():
                self.set_status(False,'Wind 连接失败，请登录客户端后重启看板。')
                return
            self.set_status(True,'Wind 已连接。仅在规则触发且数据可用时显示买入/卖出；时间口径见设置。')
            self.load_name_directory(api)
            next_quote = 0
            while not self.stop.is_set():
                if not api.isconnected():
                    self.set_status(False,'Wind 已断开，请登录客户端后重启看板。')
                    return
                self.resolve_commands(api)
                if time.monotonic() >= next_quote:
                    self.refresh_quotes(api)
                    next_quote = time.monotonic()+self.poll_seconds
                with self.lock:
                    symbols = list(self.symbols)
                for symbol in symbols:
                    if self.stop.is_set():
                        break
                    if time.monotonic() >= self.history_due.get(symbol,0):
                        success = self.refresh_swing_history(api,symbol)
                        self.history_due[symbol] = time.monotonic()+(60 if success else 300)
                        # Long initial history requests must not starve quote polling for other symbols.
                        if time.monotonic() >= next_quote:
                            self.refresh_quotes(api)
                            next_quote = time.monotonic()+self.poll_seconds
                        self.resolve_commands(api)
                self.stop.wait(.2)
        except ImportError:
            self.set_status(False,'当前 Python 找不到 WindPy，请使用已验证连接成功的 Python。')
        except Exception as error:
            self.set_status(False,'Wind 接口异常（'+type(error).__name__+'），请检查连接后重启。')
        finally:
            if api is not None:
                try:
                    api.stop()
                except Exception:
                    pass
