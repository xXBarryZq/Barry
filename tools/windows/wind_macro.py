"""Read-only Wind EDB collector with an append-only local availability ledger."""
import copy
from bisect import bisect_right
from itertools import groupby
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from wind_data import CHINA, source_date

SERIES={
    'pmi':{'name':'国家统计局制造业PMI','unit':'index','max':100},
    'dr007':{'name':'DR007','unit':'percent','max':50},
    'omo7d':{'name':'央行7天逆回购操作利率（当日适用）','unit':'percent','max':50},
}
# Supplied by the user from Wind's generator on 2026-10-28; account access still must be tested locally.
DEFAULT_CODES={'pmi':'M0017126','dr007':'M0041502','omo7d':'M0325686'}


def edb_code(value):
    value=str(value or '').strip()
    if len(value)>4000:raise ValueError('查询语句过长，请只复制该指标的EDB查询')
    match=re.search(r'\bw\.edb\s*\(\s*([\'"])([A-Za-z]{1,3}\d{4,12})\1',value)
    code=(match.group(2) if match else value).upper()
    if not re.fullmatch(r'[A-Z]{1,3}\d{4,12}',code):raise ValueError('请输入该指标的EDB编号或单指标w.edb(...)语句')
    return code


def config_value(raw):
    if not isinstance(raw,dict):raise ValueError('宏观配置无效')
    enabled=raw.get('enabled',False)
    if not isinstance(enabled,bool):raise ValueError('自动更新开关无效')
    try:interval=int(raw.get('refreshSeconds',900))
    except (ValueError,TypeError):raise ValueError('更新间隔须为60—3600秒')
    if isinstance(raw.get('refreshSeconds'),bool) or not 60<=interval<=3600:raise ValueError('更新间隔须为60—3600秒')
    codes=raw.get('codes',DEFAULT_CODES)
    if not isinstance(codes,dict):raise ValueError('指标编号配置无效')
    normalized={key:edb_code(codes.get(key)) if codes.get(key) else '' for key in SERIES}
    filled=[v for v in normalized.values() if v]
    if len(filled)!=len(set(filled)):raise ValueError('三个指标必须使用各自不同的EDB编号')
    if enabled and len(filled)!=3:raise ValueError('开启自动更新前，请填入三项指标编号')
    return {'enabled':enabled,'refreshSeconds':interval,'codes':normalized}


def binding_hash(config):
    return hashlib.sha256(json.dumps(config['codes'],sort_keys=True).encode()).hexdigest()


def response_diagnostics(response):
    """Bounded types/counts, not arbitrary vendor messages or complete histories."""
    times=list(getattr(response,'Times',None) or []);data=getattr(response,'Data',None) or []
    def sample(v):
        if v is None:return None
        if isinstance(v,bool):return {'type':'bool'}
        try:
            value=float(v)
            if math.isfinite(value):return value
            return {'type':'nonfinite'}
        except (TypeError,ValueError):
            if isinstance(v,str) and len(v)<=32 and re.fullmatch(r'[\d\s.,%+eE-]+',v):return {'numericText':v}
            return {'type':type(v).__name__}
    columns=[]
    for values in data[:8]:
        if not isinstance(values,(list,tuple)):
            columns.append({'container':type(values).__name__});continue
        counts={}
        for v in values:
            if v is None:kind='none'
            elif isinstance(v,bool):kind='bool'
            else:
                try:kind='finite_numeric_text' if isinstance(v,str) and math.isfinite(float(v)) else 'finite_number' if math.isfinite(float(v)) else 'nonfinite'
                except (TypeError,ValueError):kind='other_text' if isinstance(v,str) else 'other_type'
            counts[kind]=counts.get(kind,0)+1
        columns.append({'length':len(values),'types':counts,'lastSamples':[sample(v) for v in values[-3:]]})
    return {'errorCode':getattr(response,'ErrorCode',None),'fields':[str(f)[:80] for f in (getattr(response,'Fields',None) or [])[:8]],
      'codes':[str(c)[:32] for c in (getattr(response,'Codes',None) or [])[:8]],'rows':len(times),
      'firstTimes':[str(t)[:40] for t in times[:2]],'lastTimes':[str(t)[:40] for t in times[-2:]],'columns':columns}


def fetch_macro(api,config,begin,end,options=''):
    """EDB Times are observation periods; never call them publication timestamps."""
    config=config_value(config);first,last=date.fromisoformat(begin),date.fromisoformat(end)
    if options not in ('','Fill=Previous'):raise ValueError('不支持的宏观查询选项')
    if last<first or (last-first).days>4300:raise ValueError('宏观查询日期范围无效或超过4300天')
    result={'schema':'barry-wind-macro-snapshot-v1','begin':begin,'end':end,'bindingHash':binding_hash(config),
      'collectorSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'series':[],'historicalReleaseTimesVerified':False,
      'note':'普通EDB历史为当前可读版本，Times按观察期处理；未验证历史首次公布时间/版本。'}
    for key,meta in SERIES.items():
        code=config['codes'][key];item={'key':key,'code':code,**meta,'options':options,'points':[]}
        try:
            if not code:raise ValueError('未配置指标编号')
            response=api.edb(code,begin,end,options) # Fill is permitted for diagnosis only; normal collectors use ''.
            item['diagnostics']=response_diagnostics(response)
            error=getattr(response,'ErrorCode',None);item['errorCode']=error
            if error!=0:raise ValueError('Wind EDB错误码：'+str(error))
            times=list(getattr(response,'Times',None) or []);values=getattr(response,'Data',None) or []
            codes=[str(v).upper() for v in (getattr(response,'Codes',None) or [])]
            item['returnedRows']=len(times)
            if codes and codes!=[code]:raise ValueError('EDB返回编号与申请指标不一致')
            if len(values)!=1 or len(values[0])!=len(times):raise ValueError('EDB数值与观察期维度不匹配')
            seen=set();invalid=0
            for observed,raw in zip(times,values[0]):
                day=source_date(observed).isoformat()
                if not begin<=day<=end:raise ValueError('EDB返回范围外的观察期')
                if day in seen:raise ValueError('EDB同一观察期重复，需核对版本口径')
                seen.add(day)
                try:value=float(raw)
                except (TypeError,ValueError):invalid+=1;continue
                if not math.isfinite(value):invalid+=1;continue
                if isinstance(raw,bool) or not 0<=value<=meta['max'] or (key=='pmi' and value==0):raise ValueError('数值超出该指标范围，请核对编号和单位')
                item['points'].append({'observationDate':day,'value':value})
            item['invalidRows']=invalid
            if not item['points']:raise ValueError('EDB没有有效数值；请检查编号、区间和接口权限')
            # Missing rows remain missing, never a forward-filled rate or PMI.
            item['points'].sort(key=lambda p:p['observationDate'])
        except Exception as error:
            item['points']=[]
            item['error']=str(error) if isinstance(error,ValueError) else 'EDB接口异常类型：'+type(error).__name__
        result['series'].append(item)
    result['readAt']=int(datetime.now(CHINA).timestamp()*1000)
    return result


def observed_context(records):
    """Each revision becomes usable only at local first observation, never earlier."""
    macro=[];dr_values={};omo_values={};paired={}
    for seen,batch in groupby(sorted(records,key=lambda x:(x['seenAt'],x['id'])),key=lambda r:r['seenAt']):
        pmi={}
        for r in batch:
            if r['key']=='pmi':pmi[r['observationDate']]=r
            elif r['key']=='dr007':dr_values[r['observationDate']]=r
            else:omo_values[r['observationDate']]=r
        for r in pmi.values():
            day=date.fromisoformat(r['observationDate'])
            if (day+timedelta(days=1)).month==day.month:continue
            macro.append({'indicator':'cn_pmi','observationDate':r['observationDate'],'value':r['value'],
              'availableAt':seen,'vintage':'locally_observed','source':'Wind EDB','reference':r['code']})
        policy_dates=sorted(omo_values)
        for day,dr in dr_values.items():
            index=bisect_right(policy_dates,day)-1
            if index<0:continue
            # This configured series is a policy-rate state, valid until its next change.
            # Carry the known rate state, never a future rate or missing DR007 value.
            omo=omo_values[policy_dates[index]]
            signature=(dr['value'],omo['value'],omo['observationDate'])
            if paired.get(day)==signature:continue
            paired[day]=signature
            macro.append({'indicator':'cn_funding','observationDate':day,'value':dr['value'],
              'referenceValue':omo['value'],'availableAt':max(dr['seenAt'],omo['seenAt']),
              'referenceObservationDate':omo['observationDate'],'vintage':'locally_observed','source':'Wind EDB','reference':dr['code']+' / '+omo['code']})
    return {'schema':'barry-context-v1','macro':macro,'events':[],'coverage':[],'issues':[],
      'macroSourcePolicy':'locally_observed','scope':'从本机实际首次读取起生效，保留修订；不能据此还原采集前的历史首发版本。'}


class MacroStore:
    def __init__(self,directory=None):
        self.directory=Path(directory) if directory is not None else None
        self.lock=threading.RLock();self.worker=None;self.due=0;self.loading=False;self.manual_pending=False;self.message='尚未配置Wind宏观指标编号。'
        self.config=config_value({'enabled':self.directory is not None});self.config_issue=''
        if self.directory:
            self.directory.mkdir(parents=True,exist_ok=True)
            path=self.directory/'wind-macro.json'
            if path.exists():
                try:
                    if path.stat().st_size>32_000:raise ValueError('配置过大')
                    self.config=config_value(json.loads(path.read_text(encoding='utf-8')))
                except (ValueError,OSError):self.config_issue='宏观配置无法读取，原文件已保留；请在宏观数据页重新核对。'
        if self.directory and not self.config_issue:self.message='已预置Wind指标编号，等待自动读取。' if self.config['enabled'] else '宏观自动更新已关闭。'
        self.db=sqlite3.connect(str(self.directory/'wind-macro.sqlite3') if self.directory else ':memory:',check_same_thread=False,timeout=5)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS observations (
          id INTEGER PRIMARY KEY, binding TEXT NOT NULL, series_key TEXT NOT NULL, code TEXT NOT NULL,
          observation_date TEXT NOT NULL, value REAL NOT NULL, seen_at INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS observed_lookup ON observations(binding,series_key,observation_date,id);
          CREATE TABLE IF NOT EXISTS last_batch (binding TEXT PRIMARY KEY, payload TEXT NOT NULL);''')

    def configure(self,raw):
        value=config_value(raw)
        with self.lock:
            if self.directory:
                path=self.directory/'wind-macro.json';temp=path.with_suffix('.tmp')
                temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temp.replace(path)
            self.config=value;self.config_issue='';self.due=0;self.manual_pending=False
            self.message='已保存，等待本机自动读取。' if value['enabled'] else '自动更新已关闭，保留已采集资料。'
        return value

    def append(self,batch,config=None):
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            config=config or self.config;binding=binding_hash(config)
            if batch.get('bindingHash')!=binding:raise ValueError('宏观编号配置已改变，丢弃旧查询结果')
            if any(s.get('options','') for s in batch['series']):raise ValueError('诊断填充结果不能写入正式宏观留档')
            last=self.db.execute('SELECT MAX(seen_at) FROM observations').fetchone()[0]
            if last is not None and batch['readAt']<last:raise ValueError('电脑时钟早于已保存记录，暂停写入以免倒置可获得时间')
            for series in batch['series']:
                if series.get('error'):continue
                for p in series['points']:
                    previous=self.db.execute('SELECT value FROM observations WHERE binding=? AND series_key=? AND observation_date=? ORDER BY id DESC LIMIT 1',
                      (binding,series['key'],p['observationDate'])).fetchone()
                    if previous is not None and previous['value']==p['value']:continue
                    self.db.execute('INSERT INTO observations(binding,series_key,code,observation_date,value,seen_at) VALUES(?,?,?,?,?,?)',
                      (binding,series['key'],series['code'],p['observationDate'],p['value'],batch['readAt']))
            self.db.execute('INSERT OR REPLACE INTO last_batch VALUES(?,?)',(binding,json.dumps(batch,ensure_ascii=False,allow_nan=False)))

    def context(self):
        with self.lock:
            rows=self.db.execute('SELECT id,series_key AS key,code,observation_date AS observationDate,value,seen_at AS seenAt FROM observations WHERE binding=? ORDER BY seen_at,id',
              (binding_hash(self.config),)).fetchall()
        return observed_context([dict(r) for r in rows])

    def snapshot(self):
        try:return self._snapshot()
        except (sqlite3.Error,OSError,ValueError):
            return {'config':copy.deepcopy(self.config),'loading':False,'readAt':None,'series':[],
              'message':'宏观留档暂不可读；行情继续，宏观背景等待资料。','liveContext':observed_context([]),
              'strictHistoricalReady':False,'historyNote':'本机宏观留档需要检查，不能据此生成宏观信号。'}

    def _snapshot(self):
        with self.lock:
            row=self.db.execute('SELECT payload FROM last_batch WHERE binding=?',(binding_hash(self.config),)).fetchone()
            batch=json.loads(row[0]) if row else None
            config=copy.deepcopy(self.config);loading=self.loading;message=self.config_issue or self.message
            context=self.context()
        latest=[]
        # Send only current background observations to the web page, not the archive.
        for indicator in ('cn_pmi','cn_funding'):
            own=[r for r in context['macro'] if r['indicator']==indicator]
            dates=sorted({r['observationDate'] for r in own})[-2:]
            for day in dates:latest.append(max((r for r in own if r['observationDate']==day),key=lambda r:r['availableAt']))
        context['macro']=latest
        series=[]
        for key,meta in SERIES.items():
            s=next((s for s in (batch or {}).get('series',[]) if s['key']==key),{})
            point=(s.get('points') or [None])[-1]
            series.append({'key':key,'name':meta['name'],'unit':meta['unit'],'code':config['codes'][key],
              'latest':point,'rows':len(s.get('points') or []),'error':s.get('error'),'invalidRows':s.get('invalidRows',0)})
        return {'config':config,'loading':loading,'message':message,'readAt':batch.get('readAt') if batch else None,
          'series':series,'liveContext':context,'strictHistoricalReady':False,
          'historyNote':'已读取的EDB日期是观察期，历史首发时间/版本未验证；本机留档只从实际读取时刻起用于后续研究。'}

    def refresh(self):
        with self.lock:self.due=0;self.manual_pending=True

    def tick(self):
        try:self._tick()
        except Exception as error:
            with self.lock:
                self.loading=False;self.due=time.monotonic()+300
                self.message='宏观服务异常（'+type(error).__name__+'），稍后重试；行情轮询继续。'

    def _tick(self):
        with self.lock:
            result=self.worker.poll() if self.worker else None
            if result:
                self.loading=False
                if result['job']['binding']==binding_hash(self.config):
                    try:
                        if 'error' in result:raise ValueError(result['error'])
                        self.append(result['data'])
                        errors=[s for s in result['data']['series'] if s.get('error')]
                        self.message='宏观读取完成，已保留首次观察和数值修订。' if not errors else '部分宏观指标读取失败；请查看各项原因。'
                    except (ValueError,sqlite3.Error) as error:self.message='宏观读取未完成：'+str(error)
                    self.due=time.monotonic()+self.config['refreshSeconds']
            if self.config_issue or self.loading:return
            if not self.manual_pending and (not self.config['enabled'] or time.monotonic()<self.due):return
            if self.worker is None:
                from history_worker import HistoryWorker
                self.worker=HistoryWorker(timeout=60)
            today=datetime.now(CHINA).date()
            if self.worker.submit({'kind':'macro','binding':binding_hash(self.config),'config':copy.deepcopy(self.config),
              'begin':(today-timedelta(days=120)).isoformat(),'end':today.isoformat()}):
                self.loading=True;self.manual_pending=False;self.message='正在本机独立进程读取宏观，行情轮询继续。'

    def close(self):
        if self.worker:self.worker.close()

    def collect_history(self,begin,end,worker=None):
        """Use the same child-process collector for a one-off historical request."""
        if not all(self.config['codes'].values()):raise ValueError('请先在新版看板“宏观数据”页填入三项EDB编号，再启动自动宏观回测')
        from history_worker import HistoryWorker
        worker=worker or HistoryWorker(timeout=90)
        try:
            if not worker.submit({'kind':'macro','config':copy.deepcopy(self.config),'begin':begin,'end':end}):
                raise ValueError('宏观查询进程正忙，请稍后重试')
            while True:
                result=worker.poll()
                if result:
                    if 'error' in result:raise ValueError(result['error'])
                    self.append(result['data']);return result['data']
                time.sleep(.1)
        finally:worker.close()
