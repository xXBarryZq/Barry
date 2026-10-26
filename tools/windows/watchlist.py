"""Wind-backed editable watchlist, persisted locally without prices or credentials."""

import copy
import importlib
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta

from swing_data import fetch_history
from history_worker import HistoryWorker
from research import ResearchStore
from security_catalog import SecurityCatalog, SettingsStore, candidates, instrument, normalize_query
from wind_data import CHINA, bars_from_result, cells, get_zone, merge_bars, quote_from_result, finite

DEFAULT_SYMBOLS = ('SH.510300', 'SH.600519', 'SHFE.rb', 'US.AAPL', 'US.SPY')


class WindDashboard:
    def __init__(self, symbols=DEFAULT_SYMBOLS, poll_seconds=5, state_path=None):
        self.catalog = SecurityCatalog()
        self.store = SettingsStore(state_path)
        self.research = ResearchStore(self.store.path.with_name("research.json") if self.store.path is not None else None)
        self.stop, self.lock = threading.Event(), threading.RLock()
        self.instance_id=uuid.uuid4().hex
        self.last_quote_read=None;self.last_quote_attempt=None;self.quote_polls=0
        self.history_cursor=0;self.directory_due=0
        self.connected, self.message = False, '正在连接本机 Wind……'
        self.poll_seconds = max(3, poll_seconds)
        self.commands, self.pending, self.resolve_errors = queue.Queue(), set(), {}
        self.retry_due, self.last_attempt, self.search_previews = {}, {}, {}
        self.date_supported, self.date_due, self.history_due = {}, {}, {}
        self.zones = {'usQuoteZone': self.store.data.get('usQuoteZone', 'unknown'),
                      'usBarZone': self.store.data.get('usBarZone', 'unknown')}
        self.catalog.import_rows(self.store.data.get('directory', []))
        saved = self.store.data.get('watchlist')
        if saved is not None:
            symbols = []
            for row in saved:
                item = instrument(row['windCode'], row['name'])
                item.update({key:row[key] for key in ('nameSource','namePending') if key in row})
                self.catalog.add(item)
                symbols.append(item['id'])
        if len(symbols) > 50 or any(self.catalog.get(symbol) is None for symbol in symbols):
            raise ValueError('自选列表无效或超过 50 个品种')
        self.symbols = list(dict.fromkeys(symbols))
        self.states = {symbol:self.empty_state() for symbol in self.symbols}

    @staticmethod
    def empty_state():
        return {'quote':None, 'bars':[], 'quoteIssue':'', 'historyIssue':'', 'missingBars':0, 'historyToken':uuid.uuid4().hex, 'historyLoading':False, 'dailyBars':[], 'hourlyBars':[], 'calendar':[], 'expectedDailyDate':None, 'swingRevision':0, 'swingIssue':'正在补充日线和60分钟历史。'}

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
            item.update(quote=state['quote'], bars=state['bars'], dataIssue=issue, missingBars=state['missingBars'], dailyBars=state['dailyBars'], hourlyBars=state['hourlyBars'], calendar=state['calendar'], expectedDailyDate=state['expectedDailyDate'], swingRevision=state['swingRevision'], swingIssue=state['swingIssue'], historyToken=state['historyToken'], historyLoading=state['historyLoading'], research=self.research.snapshot(item['windCode']))
            items.append(item)
        return {'instanceId':self.instance_id, 'mode':'live', 'provider':'wind-local', 'computeStrategies':True,
                'source':'Wind 本机行情 · 5 秒轮询', 'serverTime':int(time.time()*1000),
                'status':{'connected':connected,'message':message,'lastQuoteReadAt':self.last_quote_read,'lastQuoteAttemptAt':self.last_quote_attempt,'quotePolls':self.quote_polls,'historyLoading':any(state['historyLoading'] for state in states.values())}, 'instruments':items,
                'editableWatchlist':True, 'directoryStatus':self.catalog.a_share_status,
                'settings':zones}

    def persist(self, symbols=None, extra=None):
        ids = self.symbols if symbols is None else symbols
        self.store.save({'watchlist':[self.catalog.get(symbol) for symbol in ids], **(extra or {})})

    def add_watch(self, value):
        item = self.catalog.get(value)
        if item is None:
            raise ValueError('请先输入六位代码搜索，在结果中点击添加。')
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
                    rows = [{'wind_code':item['windCode'], 'sec_name':item['name']} for item in self.catalog.items.values() if not item.get('namePending')]
                    self.persist(extra={'directory':rows})
                except Exception:
                    self.catalog.items = previous
                    raise
        return count

    def search(self, text, retry=False):
        text = normalize_query(text[:80])
        possible = candidates(text)
        key = possible[0] if len(possible)==1 else text.upper()
        with self.lock:
            item = self.catalog.get(text)
            # Show a code card immediately. It is explicitly unverified, never a made-up name.
            if item is None and len(possible)==1 and len(self.catalog.items)<25000:
                try:
                    item = instrument(possible[0], possible[0].split('.')[0]+'（名称待核实）')
                    item.update(namePending=True, nameSource='代码格式识别，证券身份待核实')
                    self.catalog.add(item)
                except ValueError:
                    pass
            needs_lookup = possible and (item is None or item.get('namePending') or retry)
            due = self.retry_due.get(key,0)
            ready = time.monotonic() >= (self.last_attempt.get(key,0)+2 if retry else due)
            if needs_lookup and ready and key not in self.pending and len(self.pending)<20 and self.connected:
                self.pending.add(key)
                self.commands.put((key,text))
            pending = key in self.pending
            active = set(self.symbols)
            results = self.catalog.search(text)
            if item and not any(row['id']==item['id'] for row in results):
                results = [item,*results][:15]
            note = self.resolve_errors.get(key,'')
            if not note:
                note = '输入代码后自动识别股票/ETF，点击添加开始读取行情。' if item else self.catalog.a_share_status
            if retry and not ready and not pending:
                note = '距上次查询未满2秒，请稍后点击重新查询。'
            if not self.connected:
                note = 'Wind尚未连接。代码卡片仅供确认，添加后等待真实行情。'
            if text.isdecimal() and len(text)!=6:
                note = '请继续输入完整六位股票或ETF代码，保留开头的0。'
            response = []
            for row in results:
                preview = self.search_previews.get(row['windCode'])
                response.append({**row, 'added':row['id'] in active,
                                 'preview':preview if preview and int(time.time()*1000)-preview['readAt']<120000 else None})
        return {'results':response,'pending':pending,'message':note,
                'canRetry':bool(possible and key in self.resolve_errors and not pending),
                'scope':'六位代码自动识别、Wind名称目录、已导入目录'}

    @staticmethod
    def lookup_name(api, code):
        try:
            result = api.wss(code, 'sec_name', '')
            error = getattr(result,'ErrorCode',None)
            if error != 0:
                safe = str(error) if isinstance(error,int) else '缺失'
                return None, 'WSS名称接口错误码 '+safe
            codes = [str(value).upper() for value in (getattr(result,'Codes',None) or [])]
            if codes and code.upper() not in codes:
                return None, 'WSS返回的证券代码不匹配'
            name = cells(result, codes.index(code.upper()) if codes else 0).get('SEC_NAME')
            if isinstance(name,str) and name.strip().casefold() not in ('','nan','none','null','n/a','--'):
                return name.strip(), ''
            return None, 'WSS调用成功，但SEC_NAME名称字段为空或缺失'
        except Exception as error:
            return None, 'WSS名称接口调用异常（'+type(error).__name__+'）'

    def resolve_commands(self, api):
        for _ in range(2):
            try:
                key,text = self.commands.get_nowait()
            except queue.Empty:
                break
            found,diagnostics = False,[]
            for code in candidates(text):
                name,issue = self.lookup_name(api,code)
                if name:
                    with self.lock:
                        item = self.catalog.get(code) or instrument(code,name)
                        item.update(name=name,namePending=False,nameSource='Wind SEC_NAME')
                        self.catalog.add(item)
                        if item['id'] in self.symbols:
                            try:
                                self.persist()
                            except (OSError,ValueError):
                                self.message = '名称已读取，但本机配置未能保存；行情继续运行，请检查配置文件权限。'
                    found = True
                    break
                diagnostics.append(issue)
                # Name and quote entitlements are independent. A name failure must not hide a valid price.
                try:
                    result = api.wsq(code,'rt_last')
                    index = self.index(result,code)
                    last = finite(cells(result,index).get('RT_LAST'),positive=True)
                    if last is not None:
                        with self.lock:
                            item = self.catalog.get(code) or instrument(code,code.split('.')[0]+'（名称待核实）')
                            item.setdefault('namePending',True)
                            self.catalog.add(item)
                            self.search_previews[code] = {'price':last,'readAt':int(time.time()*1000)}
                        diagnostics.append('WSQ已返回价格；名称暂缺仍可按代码添加')
                        break
                    error = getattr(result,'ErrorCode',None)
                    diagnostics.append('WSQ价格校验未返回有效价格'+('，错误码 '+str(error) if isinstance(error,int) and error else ''))
                except Exception as error:
                    diagnostics.append('WSQ价格校验异常（'+type(error).__name__+'）')
            with self.lock:
                self.last_attempt[key] = time.monotonic()
                self.pending.discard(key)
                if found:
                    self.resolve_errors.pop(key,None)
                    self.retry_due.pop(key,None)
                else:
                    self.resolve_errors[key] = '；'.join(diagnostics[:4])+'。可按代码添加后查询；也可点击重新查询。'
                    self.retry_due[key] = time.monotonic()+10
                for mapping in (self.resolve_errors,self.retry_due,self.last_attempt,self.search_previews):
                    while len(mapping)>500:
                        mapping.pop(next(iter(mapping)))

    def load_name_directory(self, api):
        try:
            result = api.wset('sectorconstituent', 'sectorid=a001010100000000')
            if getattr(result, 'ErrorCode', None) != 0:
                raise ValueError('目录读取失败')
            fields = [str(field).upper() for field in (result.Fields or [])]
            codes, names = result.Data[fields.index('WIND_CODE')], result.Data[fields.index('SEC_NAME')]
            count = self.catalog.import_rows({'wind_code':code,'sec_name':name} for code,name in zip(codes,names))
            self.catalog.a_share_status = f'已加载 {count} 个 Wind A股名称；股票和ETF可用六位代码；美股可用完整代码，或导入名称目录。'
        except Exception:
            self.catalog.a_share_status = 'Wind A股名称目录未加载；股票和ETF仍可按六位代码添加，名称暂缺会标明。'

    @staticmethod
    def index(result, code):
        return [str(value).upper() for value in (getattr(result,'Codes',None) or [])].index(code.upper())

    def refresh_quotes(self, api, unused=None):
        with self.lock:
            self.last_quote_attempt=int(time.time()*1000)
            self.quote_polls+=1
            instruments = [self.catalog.get(symbol) for symbol in self.symbols]
        groups = {'cn':[item for item in instruments if item['market'] != 'us'],
                  'us':[item for item in instruments if item['market'] == 'us']}
        for group,items in groups.items():
            if items and not self._refresh_quote_batch(api,group,items) and len(items)>1:
                # A newly typed invalid/unentitled code must not block existing valid symbols.
                for item in items:
                    self._refresh_quote_batch(api,group,[item])
        return dict(self.date_supported)

    def _refresh_quote_batch(self, api, group, items):
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
            return False
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
                    self.last_quote_read=quote['receivedAt']
        return True

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
                    changed=any(self.states[symbol].get(key)!=value for key,value in data.items())
                    self.states[symbol].update(**data,swingIssue='',historyIssue='',historyLoading=False,swingRevision=previous['swingRevision']+int(changed))
                    if changed:self.states[symbol]['historyToken']=uuid.uuid4().hex
            return True
        except Exception as error:
            message = str(error) if isinstance(error,ValueError) else '日线/60分钟历史读取失败，请核对 WSD、WSI 和交易日历权限。'
            with self.lock:
                if symbol in self.states:
                    self.states[symbol]['swingIssue'] = message
            return False

    def service_history(self, worker):
        result=worker.poll()
        if result:
            job=result['job']
            if job['kind']=='directory':
                if 'data' in result:
                    count=self.catalog.import_rows(result['data']['directory'])
                    self.catalog.a_share_status=f'已加载{count}个Wind A股名称；股票和ETF可用六位代码。'
                    self.directory_due=float('inf')
                else:
                    self.catalog.a_share_status=result['error']
                    self.directory_due=time.monotonic()+300
            else:
                symbol=job['symbol']
                with self.lock:
                    state=self.states.get(symbol)
                    # Removed/re-added symbols or timezone resets cannot consume an old response.
                    if state is not None and state['historyToken']==job['token']:
                        state['historyLoading']=False
                        if 'data' in result:
                            data=result['data']
                            changed=any(state.get(key)!=value for key,value in data.items())
                            state.update(**data,swingIssue='',historyIssue='')
                            if changed:
                                state['swingRevision']+=1;state['historyToken']=uuid.uuid4().hex
                        else:
                            state['swingIssue']=result['error']
                        self.history_due[symbol]=time.monotonic()+(60 if 'data' in result else 300)
        if worker.job is not None or self.stop.is_set():
            return
        if time.monotonic()>=self.directory_due:
            worker.submit({'kind':'directory'})
            return
        with self.lock:
            symbols=list(self.symbols)
        for offset in range(len(symbols)):
            index=(self.history_cursor+offset)%len(symbols)
            symbol=symbols[index];item=self.catalog.get(symbol)
            if time.monotonic()<self.history_due.get(symbol,0):continue
            if item['market']=='futures' or (item['market']=='us' and self.zones['usBarZone']=='unknown'):
                with self.lock:
                    if symbol in self.states:
                        self.states[symbol]['swingIssue']='期货波段规则尚未验证。' if item['market']=='futures' else 'Wind美股历史时间口径尚未确认。'
                        self.history_due[symbol]=time.monotonic()+300
                continue
            with self.lock:
                state=self.states.get(symbol)
                if state is None:continue
                prior={key:copy.deepcopy(state[key]) for key in ('dailyBars','hourlyBars','calendar','expectedDailyDate')}
                job={'kind':'history','symbol':symbol,'token':state['historyToken'],'item':item,'state':prior,
                     'zone':self.zones['usBarZone'] if item['market']=='us' else 'Asia/Shanghai'}
                if worker.submit(job):
                    state['historyLoading']=True;self.history_cursor=(index+1)%len(symbols)
                break

    def run(self, history_worker=None):
        api=None;worker=history_worker
        try:
            api=importlib.import_module('WindPy').w
            worker=worker or HistoryWorker()
            next_quote=0
            while not self.stop.is_set():
                try:
                    if not self.connected or not api.isconnected():
                        self.set_status(False,'Wind未连接，正在自动重连；旧报价不用于新信号。')
                        result=api.start()
                        if getattr(result,'ErrorCode',None)!=0 or not api.isconnected():
                            self.stop.wait(5);continue
                        next_quote=0
                    self.set_status(True,'Wind已连接。报价持续轮询，日线和60分钟历史在独立进程加载。')
                    if time.monotonic()>=next_quote:
                        self.refresh_quotes(api);next_quote=time.monotonic()+self.poll_seconds
                    self.resolve_commands(api)
                    self.service_history(worker)
                except Exception as error:
                    self.set_status(False,'Wind接口异常（'+type(error).__name__+'），5秒后自动重试。')
                    self.stop.wait(5)
                self.stop.wait(.2)
        except ImportError:
            self.set_status(False,'当前Python找不到WindPy，请使用已验证连接成功的Python。')
        finally:
            if worker is not None:worker.close()
            if api is not None:
                try:api.stop()
                except Exception:pass
