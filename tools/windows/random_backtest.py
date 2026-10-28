"""Read-only, fixed-cohort Wind research. No orders and no dashboard rule changes."""
import argparse
import copy
from datetime import date, datetime, time, timedelta
import hashlib
import html
import importlib
import json
import os
from pathlib import Path
import random
import re
import sys
import webbrowser

from swing_data import rows
from wind_data import CHINA, cells, finite, source_date, timestamp
from context_data import read_context

FIELDS='open,high,low,close,volume'
SLOTS=('10:30','11:30','14:00','15:00')


class DataReadError(ValueError):
    def __init__(self,message,diagnostics):
        super().__init__(message)
        self.diagnostics=diagnostics


def result_info(result):
    """Metadata and counts only; do not export full vendor history or credentials."""
    fields=list(getattr(result,'Fields',None) or [])
    times=list(getattr(result,'Times',None) or [])
    data=getattr(result,'Data',None) or []
    counts={}
    for i,field in enumerate(fields):
        values=data[i] if i<len(data) and data[i] is not None else []
        name=str(field).upper()
        counts[name]={'returned':len(values),'finiteNonnegative':sum(finite(v) is not None for v in values),
                      'positive':sum(finite(v,positive=True) is not None for v in values)}
    return {'errorCode':getattr(result,'ErrorCode',None),'fields':[str(f) for f in fields],
            'rows':len(times),'firstTimes':[str(t) for t in times[:3]],'lastTimes':[str(t) for t in times[-3:]],'values':counts}


def checked(result,stage):
    code=getattr(result,'ErrorCode',None)
    if code!=0:raise ValueError(stage+'失败，Wind错误码：'+str(code))
    return result


def stock_code(value):
    value=str(value).strip().upper()
    return bool(re.fullmatch(r'(?:60[0135]\d{3}|688\d{3})\.SH|(?:00[0123]\d{3}|30[01]\d{3})\.SZ',value))


def history_universe(api,day,count,seed):
    result=checked(api.wset('sectorconstituent','date='+day+';sectorid=a001010100000000'),'历史沪深A股目录')
    fields=[str(v).upper() for v in (getattr(result,'Fields',None) or [])]
    if not {'WIND_CODE','SEC_NAME'}.issubset(fields):raise ValueError('历史股票池缺少WIND_CODE或SEC_NAME字段')
    if 'DATE' in fields:
        dates={source_date(v).isoformat() for v in result.Data[fields.index('DATE')]}
        if dates!={day}:raise ValueError('股票池返回日期与申请的历史日期不一致；停止抽样，避免使用今天的股票池')
    codes=result.Data[fields.index('WIND_CODE')];names=result.Data[fields.index('SEC_NAME')]
    universe={str(code).strip().upper():str(name)[:120] for code,name in zip(codes,names) if stock_code(code)}
    ordered=sorted(universe)
    if len(ordered)<count:raise ValueError('历史股票池数量不足；不改用当前股票池或内置示例')
    selected=random.Random(seed).sample(ordered,count)
    digest=hashlib.sha256('\n'.join(ordered).encode()).hexdigest()
    verified='接口返回的DATE与申请日期一致。' if 'DATE' in fields else '接口未返回DATE，无法独立确认目录的历史时点，存在幸存者偏差风险。'
    note=(f'按{day}申请当时沪深A股目录（不含北交所），共{len(ordered)}只，排序后固定种子等概率抽取。'
          '只研究这一开始时点的固定队列，不包含其后新上市股票；后续退市或数据失败的样本不替换。'+verified)
    return [{'code':code,'name':universe[code]} for code in selected],{
        'date':day,'count':len(ordered),'sha256':digest,'dateFieldVerified':'DATE' in fields,'note':note}


def calendar(api,begin,end):
    result=checked(api.tdays(begin,end,'TradingCalendar=SSE'),'沪深交易日历')
    if not getattr(result,'Data',None):raise ValueError('Wind未返回交易日历')
    values=sorted({source_date(v).isoformat() for v in result.Data[0]})
    if not values:raise ValueError('交易日历为空')
    return values


def placeholder(day,slot=None):
    clock=datetime.combine(date.fromisoformat(day),time.fromisoformat(slot) if slot else time())
    return {'date':day,'slot':slot,'time':timestamp(clock,CHINA),'open':None,'high':None,'low':None,'close':None,'volume':None}


def fetch_stock(api,stock,dates,start,end,daily_source='auto'):
    diagnostics={'code':stock['code'],'startDate':start,'endDate':end,'hourlyRequests':[]}
    try:
        if daily_source=='hourly':return fetch_hourly_stock(api,stock,dates,start,end,diagnostics)
        return _fetch_stock(api,stock,dates,start,end,diagnostics)
    except ValueError as error:
        if daily_source=='auto' and diagnostics.get('dailyUnavailable'):
            diagnostics['fallbackReason']=str(error)
            print('    WSD日线不可用，改用同次复权小时查询合成完整日线；研究区间保持不变。',flush=True)
            try:return fetch_hourly_stock(api,stock,dates,start,end,diagnostics)
            except ValueError as fallback_error:raise DataReadError(str(fallback_error),diagnostics) from fallback_error
        raise DataReadError(str(error),diagnostics) from error


def _fetch_stock(api,stock,dates,start,end,diagnostics):
    begin=dates[0];cutoff=date.fromisoformat(end)+timedelta(days=1)
    now=timestamp(datetime.combine(cutoff,time()),CHINA)
    adjusted=api.wsd(stock['code'],FIELDS,begin,end,'PriceAdj=F')
    diagnostics['adjustedDaily']=result_info(adjusted)
    checked(adjusted,'前复权日线 '+stock['code'])
    daily_by={b['date']:b for b in rows(adjusted,'daily',now,CHINA,cutoff.isoformat()) if begin<=b['date']<=end}
    diagnostics['validAdjustedDaily']=sum(bool(b.get('valid')) for b in daily_by.values())
    if not diagnostics['validAdjustedDaily']:
        diagnostics['dailyUnavailable']=True
        raise ValueError('前复权日线返回'+str(diagnostics['adjustedDaily']['rows'])+'行，但没有完整有效的OHLCV；小时数据尚未查询。请查看接口诊断中的各字段有效数量')
    raw=api.wsd(stock['code'],'close',begin,end,'')
    diagnostics['rawDaily']=result_info(raw)
    checked(raw,'原始收盘价 '+stock['code'])
    raw_by={source_date(v).isoformat():finite(cells(raw,i).get('CLOSE'),positive=True) for i,v in enumerate(getattr(raw,'Times',None) or [])}
    factors={day:b['close']/raw_by[day] for day,b in daily_by.items() if b['close'] and raw_by.get(day)}
    diagnostics['matchingAdjustmentDates']=len(factors)
    if not factors:
        diagnostics['dailyUnavailable']=True
        raise ValueError('日线复权匹配失败：前复权日线有效收盘'+str(sum(bool(b['close']) for b in daily_by.values()))+
                         '条，原始日线有效收盘'+str(sum(v is not None for v in raw_by.values()))+'条，共同有效日期0个；小时数据尚未查询，不能据此判断没有小时行情权限')
    found={};bad_slots=set()
    diagnostics['conversion']={'parsedRows':0,'outsideStudy':0,'missingAdjustment':0,'matchedSlots':0}
    chunk=date.fromisoformat(start);last=date.fromisoformat(end)
    while chunk<=last:
        chunk_end=min(last,chunk+timedelta(days=179))
        print('    读取60分钟：'+chunk.isoformat()+' 至 '+chunk_end.isoformat(),flush=True)
        response=api.wsi(stock['code'],FIELDS,chunk.isoformat()+' 09:30:00',chunk_end.isoformat()+' 15:00:00','BarSize=60')
        info=result_info(response)
        diagnostics['hourlyRequests'].append({'begin':chunk.isoformat(),'end':chunk_end.isoformat(),**info})
        checked(response,'60分钟 '+stock['code'])
        converted=rows(response,'hourly',now,CHINA)
        diagnostics['conversion']['parsedRows']+=len(converted)
        for bar in converted:
            local=datetime.fromtimestamp(bar['time']/1000,CHINA);slot=local.strftime('%H:%M')
            if not start<=bar['date']<=end:
                diagnostics['conversion']['outsideStudy']+=1
                continue
            if slot not in SLOTS:bad_slots.add(slot);continue
            factor=factors.get(bar['date'])
            if factor is None:
                diagnostics['conversion']['missingAdjustment']+=1
                continue
            # Fetch unadjusted chunks, then use ONE daily adjustment basis for all chunks.
            for field in ('open','high','low','close'):
                if bar[field] is not None:bar[field]*=factor
            bar['slot']=slot
            found[(bar['date'],slot)]=bar
            diagnostics['conversion']['matchedSlots']+=1
        chunk=chunk_end+timedelta(days=1)
    if bad_slots:raise ValueError('Wind60分钟结束时刻与预设交易时段不符：'+','.join(sorted(bad_slots))+'；需确认后再回测')
    for (day,slot),bar in found.items():
        close=daily_by.get(day,{}).get('close')
        if slot=='15:00' and close and bar.get('close') and abs(bar['close']-close)>max(.02*factors[day],close*.003):
            raise ValueError('小时末价与同日日线收盘不一致（'+day+'）；需核对小时结束时间及原始/复权口径，停止该样本')
    diagnostics['dailySource']='Wind WSD日线'
    diagnostics['priceBasis']='前复权日线与原始小时的逐日因子'
    diagnostics['priceAdjustment']='F'
    return finish_stock(stock,daily_by,found,dates,start,end,diagnostics)


def daily_from_hours(found,dates,allow_zero_volume=False):
    """Aggregate only four complete genuine bars; missing sessions remain missing."""
    daily={}
    for day in dates:
        parts=[found.get((day,slot)) for slot in SLOTS]
        if not all(b and b.get('valid') and b['volume'] is not None and (b['volume']>=0 if allow_zero_volume else b['volume']>0) for b in parts):
            continue
        daily[day]={'date':day,'time':placeholder(day)['time'],'open':parts[0]['open'],
                    'high':max(b['high'] for b in parts),'low':min(b['low'] for b in parts),
                    'close':parts[-1]['close'],'volume':sum(b['volume'] for b in parts),
                    'valid':True,'source':'WSI四根完整60分钟合成'}
    return daily


def adjusted_hour_history(api,stock,dates,end,diagnostics):
    """A single back-adjusted response: never mix bases from separate windows."""
    begin=dates[0];cutoff=date.fromisoformat(end)+timedelta(days=1)
    print('    读取统一后复权小时：'+begin+' 至 '+end,flush=True)
    response=api.wsi(stock['code'],FIELDS,begin+' 09:30:00',end+' 15:00:00','BarSize=60;PriceAdj=B')
    diagnostics['hourlyRequests'].append({'begin':begin,'end':end,'options':'BarSize=60;PriceAdj=B',**result_info(response)})
    checked(response,'统一后复权小时 '+stock['code'])
    converted=rows(response,'hourly',timestamp(datetime.combine(cutoff,time()),CHINA),CHINA)
    found={};bad_slots=set()
    counts={'parsedRows':len(converted),'outsideStudy':0,'missingAdjustment':0,'matchedSlots':0}
    for bar in converted:
        if not begin<=bar['date']<=end:counts['outsideStudy']+=1;continue
        slot=datetime.fromtimestamp(bar['time']/1000,CHINA).strftime('%H:%M')
        if slot not in SLOTS:
            if bar.get('valid'):bad_slots.add(slot)
            continue
        bar['slot']=slot;found[(bar['date'],slot)]=bar;counts['matchedSlots']+=1
    diagnostics['conversion']=counts
    diagnostics['dailySource']='WSI四根完整60分钟合成日线'
    diagnostics['priceBasis']='同一次Wind WSI请求 PriceAdj=B（后复权）；尚未与WSD交叉核对'
    diagnostics['priceAdjustment']='B'
    if bad_slots:raise ValueError('Wind60分钟结束时刻与预设交易时段不符：'+','.join(sorted(bad_slots)))
    if not any(b.get('valid') for b in found.values()):
        raise ValueError('统一后复权小时查询没有有效OHLCV；返回'+str(len(getattr(response,'Times',None) or []))+
                         '行、时间校验接受'+str(len(converted))+'行。请查看接口诊断；不会使用未确认复权的原始价格代替')
    return found


def fetch_hourly_stock(api,stock,dates,start,end,diagnostics):
    found=adjusted_hour_history(api,stock,dates,end,diagnostics)
    complete=daily_from_hours(found,dates)
    diagnostics['completeDerivedDaily']=len(complete)
    diagnostics['warmupDerivedDaily']=sum(day<start for day in complete)
    if not complete:raise ValueError('小时数据已返回，但没有任何交易日同时具备四根完整有效的60分钟线，不能合成日线')
    valid_dates=sorted(day for day in complete if day>=start)
    if not valid_dates or valid_dates[0]>(date.fromisoformat(start)+timedelta(days=30)).isoformat():
        raise ValueError('完整小时数据未覆盖研究起点附近；不能把较短可用区间当作整个研究期，请查看接口诊断确认历史覆盖范围')
    return finish_stock(stock,complete,found,dates,start,end,diagnostics)


def fetch_benchmark(api,dates,end,daily_source='auto'):
    item={'code':'000985.CSI','name':'中证全指','isProxy':False};diagnostics={'code':item['code'],'hourlyRequests':[]}
    try:
        parsed=[]
        if daily_source!='hourly':
            result=api.wsd(item['code'],FIELDS,dates[0],end,'PriceAdj=F')
            diagnostics['adjustedDaily']=result_info(result)
            checked(result,'中证全指日线')
            parsed=rows(result,'daily',timestamp(datetime.combine(date.fromisoformat(end)+timedelta(days=1),time()),CHINA),CHINA,
                        (date.fromisoformat(end)+timedelta(days=1)).isoformat())
        by={b['date']:b for b in parsed if b.get('valid')}
        diagnostics['dailySource']='Wind WSD日线'
        if not by:
            if daily_source=='wsd':raise ValueError('中证全指WSD日线没有完整有效数据；当前模式不使用小时合成')
            found=adjusted_hour_history(api,item,dates,end,diagnostics)
            by=daily_from_hours(found,dates,allow_zero_volume=True)
            if not by:raise ValueError('中证全指没有完整日线，无法使用市场过滤；个股基础规则仍单独评估')
        if len(by)<65:raise ValueError('中证全指有效日线不足65根，无法检验市场过滤')
        return {**item,'dailyBars':[by.get(day) or placeholder(day) for day in dates],'diagnostics':diagnostics}
    except ValueError as error:
        original_error=str(error)
        if daily_source=='wsd':return {**item,'dailyBars':[],'error':original_error,'diagnostics':diagnostics}
        # A disclosed large-cap proxy, chosen for availability, not for returns.
        proxy={'code':'510300.SH','name':'沪深300ETF（市场代理）','isProxy':True,'requestedCode':item['code'],
               'role':'沪深300大盘代理，与中证全指不同；此分支作为新增探索性对照'}
        proxy_diag={'code':proxy['code'],'hourlyRequests':[],'originalBenchmark':diagnostics,'originalError':original_error}
        try:
            print('中证全指不可用，尝试已明确标注的沪深300ETF市场代理。',flush=True)
            found=adjusted_hour_history(api,proxy,dates,end,proxy_diag)
            by=daily_from_hours(found,dates)
            if len(by)<65:raise ValueError('沪深300ETF有效合成日线不足65根')
            return {**proxy,'dailyBars':[by.get(day) or placeholder(day) for day in dates],'diagnostics':proxy_diag}
        except ValueError as proxy_error:
            return {**proxy,'dailyBars':[],'error':'中证全指：'+original_error+'；市场代理：'+str(proxy_error),'diagnostics':proxy_diag}


def load_study(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('schema')!='barry-random-study-v1' or not isinstance(data.get('stocks'),list):
        raise ValueError('需要原来的“回测数据.json”，不是已导出的“回测结果.json”')
    if not data.get('plan',{}).get('startDate') or not data['plan'].get('endDate'):
        raise ValueError('原始回测数据缺少固定研究日期')
    return data


def latest_study(base):
    files=sorted(Path(base).glob('run-*/回测数据.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
    for path in files:
        try:
            data=load_study(path)
            if any(s.get('hourlyBars') and s.get('dailyBars') and not s.get('error') for s in data['stocks']):return path
        except (ValueError,OSError):continue
    raise ValueError('未找到已保存的原始回测数据。请保留原报告文件夹中的“回测数据.json”，或使用--repair-data指定其路径')


def repair_market_data(api,dataset,output,source_name='原回测'):
    """Keep the cohort and every stock bar unchanged; fetch market reference only."""
    data=copy.deepcopy(dataset)
    dates=sorted({b['date'] for s in data['stocks'] if not s.get('error') for b in s.get('dailyBars',[])})
    dates=[d for d in dates if d<=data['plan']['endDate']]
    if len(dates)<65:raise ValueError('原数据中的交易日期不足65天，无法补市场参考')
    print('复用原股票数据；保留日期 '+data['plan']['startDate']+' 至 '+data['plan']['endDate'],flush=True)
    print('只补市场参考，不重新抽样，不重新查询个股价格。',flush=True)
    data['benchmark']=fetch_benchmark(api,dates,data['plan']['endDate'])
    data['repair']={'sourceRun':source_name,'repairedAt':datetime.now(CHINA).isoformat(),'stockPricesReused':True,
                    'oldBenchmarkError':dataset.get('benchmark',{}).get('error'),
                    'marketDefinitionChanged':bool(data['benchmark'].get('isProxy')),
                    'note':'若使用沪深300ETF代理，这是因接口可用性新增的探索性对照；不得称为原中证全指模型或新的独立封存检验。'}
    save_json(output/'回测数据.json',data)
    save_json(output/'接口诊断.json',{'schema':'barry-wind-diagnostics-v1','scope':'只补市场参考，个股价格复用',
        'benchmark':data['benchmark'].get('diagnostics'),'error':data['benchmark'].get('error')})
    return data


def finish_stock(stock,daily_by,found,dates,start,end,diagnostics):
    test_dates=[d for d in dates if start<=d<=end]
    hourly=[];flat=0;invalid=0
    for day in test_dates:
        for slot in SLOTS:
            bar=found.get((day,slot)) or placeholder(day,slot)
            if bar['close'] and bar['high']==bar['low']:
                flat+=1
                bar={**bar,'open':None,'high':None,'low':None,'close':None,'volume':None}
            if not bar.get('valid',True) or bar['volume'] is None or bar['volume']<=0:
                bar={**bar,'open':None,'high':None,'low':None,'close':None,'volume':None}
            if bar['close'] is None:invalid+=1
            hourly.append(bar)
    valid_hours=[b for b in hourly if b['close'] is not None]
    diagnostics['conversion'].update({'invalidHourly':invalid,'flatHourly':flat,'validHourly':len(valid_hours),
                                      'rawReturnedRows':sum(r['rows'] for r in diagnostics['hourlyRequests'])})
    if not valid_hours:
        c=diagnostics['conversion']
        if not c['rawReturnedRows']:
            message='Wind的60分钟请求返回0行；日线数据正常。需核对本次查询区间与WSI返回，不代表已确认没有权限'
        elif not c['parsedRows']:
            message='Wind已返回'+str(c['rawReturnedRows'])+'行小时记录，但时间转换接受0行；请查看接口诊断中的原始时间，不能将其当作无行情权限'
        elif c['missingAdjustment'] and not c['matchedSlots']:
            message='小时记录已返回，但其日期没有对应的有效日线复权因子；这是日线与小时数据日期匹配失败'
        else:
            message='小时记录已匹配，但OHLCV、成交量或一字价校验后没有可成交样本（异常小时'+str(invalid)+'，一字价小时'+str(flat)+'）；请查看各字段有效数量'
        raise ValueError(message)
    early_days=[d for d in test_dates if d<= (date.fromisoformat(start)+timedelta(days=30)).isoformat()]
    if valid_hours[0]['date']>early_days[-1] and sum(bool(daily_by.get(d,{}).get('volume')) for d in early_days)>=5:
        raise ValueError('日线显示期初有交易，但小时数据未覆盖期初，疑似WSI历史被截断；不使用剩余区间冒充完整研究')
    daily=[daily_by.get(day) or placeholder(day) for day in dates]
    return {**stock,'dailyBars':daily,'hourlyBars':hourly,'calendar':test_dates,
        'diagnostics':diagnostics,
        'quality':{'invalidDaily':sum(b['close'] is None or not b.get('valid',True) for b in daily),'invalidHourly':invalid,'flatHourly':flat,
                   'expectedHourly':len(hourly),'firstHourDate':valid_hours[0]['date'],'lastHourDate':valid_hours[-1]['date'],
                   'dailySource':diagnostics.get('dailySource'),'priceBasis':diagnostics.get('priceBasis'),
                   'priceAdjustment':diagnostics.get('priceAdjustment')}}


def save_json(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')),encoding='utf-8')
    temp.replace(path)


def engine_hash(repo=None):
    repo=Path(repo) if repo else Path(__file__).resolve().parents[2]
    return hashlib.sha256(b'\n'.join((repo/p).read_bytes() for p in
        ('src/swing.js','tools/research/context.js','tools/research/evaluate.js'))).hexdigest()


def report_html(dataset,repo=None):
    repo=Path(repo) if repo else Path(__file__).resolve().parents[2]
    source=(repo/'src/swing.js').read_text(encoding='utf-8')
    context=(repo/'tools/research/context.js').read_text(encoding='utf-8')
    research=(repo/'tools/research/evaluate.js').read_text(encoding='utf-8')
    research=re.sub(r'^import[^\n]*\n','',research,flags=re.M)
    bundle=re.sub(r'^export\s+','',source+'\n'+context+'\n'+research,flags=re.M)
    bundle=re.sub(r'</script',r'<\\/script',bundle,flags=re.I)
    dataset={**dataset,'evaluationEngineSha256':engine_hash(repo)}
    data=json.dumps(dataset,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
        '<title>随机A股回测报告</title><style>body{font:16px/1.7 system-ui,sans-serif;color:#18242d;background:#f7f8fa;max-width:1400px;margin:24px auto;padding:0 20px}table{border-collapse:collapse;width:100%;display:block;overflow:auto;background:white;margin:20px 0}th,td{border:1px solid #d8dee5;padding:8px 12px;white-space:nowrap;text-align:left}th{background:#e9eef3}button{margin:4px 12px 4px 0;padding:10px 16px;cursor:pointer}p{max-width:1100px}h1{font-size:26px}h2{font-size:21px}</style>'
        '<main id="study">正在计算回测结果，请稍候……</main><script type="application/json" id="data">'+data+'</script><script>'+bundle+
        '\ntry{renderStudy(JSON.parse(document.getElementById("data").textContent));}catch(error){document.getElementById("study").textContent="计算失败："+error.message;throw error;}</script></html>')


def diagnose(api,output,today=None):
    today=today or datetime.now(CHINA).date()
    dates=calendar(api,(today-timedelta(days=60)).isoformat(),(today-timedelta(days=1)).isoformat())[-10:]
    report={'schema':'barry-wind-diagnostics-v1','collectedAt':datetime.now(CHINA).isoformat(),
            'scope':'只检查近期日线与60分钟数据转换，不是交易回测，也不能确认三年历史权限。','checks':[]}
    for stock in [{'code':'510300.SH','name':'沪深300ETF'},{'code':'600519.SH','name':'贵州茅台'}]:
        print('\n检查近期数据：'+stock['code'],flush=True)
        try:
            value=fetch_stock(api,stock,dates,dates[0],dates[-1])
            check={**stock,'ok':True,'quality':value['quality'],'diagnostics':value['diagnostics']}
            print('    近期日线与小时转换通过，有效小时：'+str(value['diagnostics']['conversion']['validHourly']),flush=True)
        except DataReadError as error:
            check={**stock,'ok':False,'error':str(error),'diagnostics':error.diagnostics}
            print('    定位结果：'+str(error),flush=True)
        report['checks'].append(check)
        d=check['diagnostics']
        for key,label in [('adjustedDaily','前复权日线'),('rawDaily','原始收盘')]:
            if key in d:print('    '+label+'：'+json.dumps(d[key],ensure_ascii=False),flush=True)
        for request in d['hourlyRequests']:print('    小时原始返回：'+json.dumps(request,ensure_ascii=False),flush=True)
        if 'conversion' in d:print('    转换计数：'+json.dumps(d['conversion'],ensure_ascii=False),flush=True)
    save_json(output/'接口诊断.json',report)
    path=output/'接口诊断.html'
    lines=[f"{c['code']} {c['name']}："+('近期数据转换通过' if c['ok'] else c['error']) for c in report['checks']]
    page=('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
          '<title>Wind回测接口诊断</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f4f6;padding:20px}</style>'
          '<h1>Wind回测接口诊断</h1><p>'+html.escape(report['scope'])+'</p>'+
          ''.join('<p>'+html.escape(line)+'</p>' for line in lines)+
          '<p>诊断已保存为同文件夹的“接口诊断.json”，只含字段、时间和计数；无需提供账号密码。</p><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></html>')
    path.write_text(page,encoding='utf-8')
    print('\n接口诊断文件：'+str((output/'接口诊断.json').resolve()),flush=True)
    return path


def collect(api,count,years,seed,output,today=None,daily_source='auto'):
    today=today or datetime.now(CHINA).date()
    requested_end=today-timedelta(days=1) # Never use a partially completed current session.
    requested_start=requested_end-timedelta(days=365*years)
    dates=calendar(api,(requested_start-timedelta(days=400)).isoformat(),requested_end.isoformat())
    study_dates=[d for d in dates if d>=requested_start.isoformat()]
    if len(study_dates)<120:raise ValueError('可用交易日不足120天，停止研究')
    start,end=study_dates[0],study_dates[-1]
    selected,universe=history_universe(api,start,count,seed)
    plan={'count':count,'yearsRequested':years,'seed':seed,'startDate':start,'endDate':end,
          'splitDate':study_dates[min(len(study_dates)-1,int(len(study_dates)*2/3))],
          'short':{'days':20,'target':.05,'drawdown':.10,'loss':.10},
          'medium':{'days':60,'target':.20,'drawdown':.20,'loss':.20},
          'sample':selected,'commission':0,'ruleVersion':'price-only-exploration-v1','engineSha256':engine_hash(),
          'dailySourcePolicy':daily_source,'benchmarkPolicy':'中证全指优先；不可用时明确使用沪深300ETF大盘代理'}
    save_json(output/'抽样计划.json',{'plan':plan,'universe':universe}) # Before any price query.
    print(f'已固定随机样本{count}只，不按收益挑选或替换。研究区间：{start} 至 {end}',flush=True)
    print('名单：'+', '.join(s['code'] for s in selected),flush=True)
    print('\n先检查近期数据链路，检查失败时不继续批量长区间查询。',flush=True)
    diagnose(api,output,today=today)
    preflight=json.loads((output/'接口诊断.json').read_text(encoding='utf-8'))
    if not any(c['ok'] for c in preflight['checks']):
        raise DataReadError('近期数据预检未通过；已停止批量回测。请打开本次文件夹中的“接口诊断.html”查看原因',
                            {'preflightBlocked':True,'checks':preflight['checks']})
    benchmark=fetch_benchmark(api,dates,end,daily_source=daily_source)
    if benchmark.get('error'):print('市场过滤暂不可用：'+benchmark['error'],flush=True)
    stocks=[]
    for i,stock in enumerate(selected):
        print(f'[{i+1}/{count}] '+stock['code']+' '+stock['name'],flush=True)
        try:
            value=fetch_stock(api,stock,dates,start,end,daily_source=daily_source)
            print('    完成；无效小时 '+str(value['quality']['invalidHourly']),flush=True)
        except KeyboardInterrupt:
            stocks.extend({**s,'error':'用户中止，未完成读取'} for s in selected[i:])
            break
        except Exception as error:
            message=str(error) if isinstance(error,ValueError) else '接口异常类型：'+type(error).__name__
            value={**stock,'error':message}
            if isinstance(error,DataReadError):value['diagnostics']=error.diagnostics
            print('    保留失败样本：'+message,flush=True)
        stocks.append(value);save_json(output/(stock['code']+'.json'),value)
    dataset={'schema':'barry-random-study-v1','plan':plan,'universe':universe,'benchmark':benchmark,
             'collectedAt':datetime.now(CHINA).isoformat(),'dataSource':'本机WindPy，只读WSET/TDAYS/WSD/WSI','stocks':stocks,
             'preflight':preflight}
    save_json(output/'回测数据.json',dataset)
    return dataset


def main(argv=None):
    parser=argparse.ArgumentParser(description='固定随机抽取沪深A股，以本机Wind历史检验价量假设。不会下单。')
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--years',type=int,default=3)
    parser.add_argument('--seed',type=int,default=20261028)
    parser.add_argument('--output',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--from-json',type=Path,help='从已有本地回测数据重新生成报告，不连接Wind')
    mode.add_argument('--diagnose',action='store_true',help='只检查近期数据并保存接口诊断，不抽样或回测20只股票')
    mode.add_argument('--repair-latest',action='store_true',help='复用最近一次原始回测数据，只补市场参考')
    mode.add_argument('--repair-data',type=Path,help='指定原回测数据，只补市场参考')
    mode.add_argument('--context-latest',action='store_true',help='复用最近一次股票数据，导入本机宏观/事件历史，不连接Wind')
    parser.add_argument('--context-dir',type=Path,help='宏观及事件CSV目录；未指定时使用本机research_context目录')
    parser.add_argument('--daily-source',choices=('auto','wsd','hourly'),default='auto',help='日线来源；auto在WSD全空时使用同次复权小时合成')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.count<=100 or not 1<=args.years<=10:parser.error('样本须1—100只，年数须1—10年')
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'BarryMarketWatch' / 'backtests'
    output=args.output or base/datetime.now(CHINA).strftime('run-%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True,exist_ok=True)
    if args.from_json or args.context_latest:
        source=args.from_json or latest_study(base)
        dataset=load_study(source)
        if args.context_latest or args.context_dir:
            context_dir=args.context_dir or base.parent/'research_context'
            dataset['context']=read_context(context_dir)
            dataset['contextRun']={'sourceRun':source.parent.name,'stockPricesReused':True,
              'sourceDataSha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'note':'新增探索性宏观/事件对照；同一股票数据不是新的独立验证。'}
            print('宏观/事件资料目录：'+str(context_dir.resolve()),flush=True)
            print('原股票数据和日期保持不变；空模板不会生成已验证的增强信号。',flush=True)
            print('导入：宏观'+str(len(dataset['context']['macro']))+'条，事件'+str(len(dataset['context']['events']))+
                  '条，覆盖声明'+str(len(dataset['context']['coverage']))+'条。',flush=True)
            if dataset['context']['issues']:print('资料校验有问题，相关模块暂停；请查看报告中的具体原因。',flush=True)
            save_json(output/'回测数据.json',dataset)
            if sys.platform=='win32' and not args.no_browser:os.startfile(context_dir)
    else:
        if args.context_dir:parser.error('--context-dir须与--from-json或--context-latest一起使用')
        if sys.platform!='win32':raise ValueError('真实Wind回测须在已安装WindPy的Windows电脑运行；云端无法访问本机终端')
        api=importlib.import_module('WindPy').w
        try:
            checked(api.start(),'连接Wind')
            if not api.isconnected():raise ValueError('Wind未连接，请保持终端登录')
            if args.diagnose:
                path=diagnose(api,output)
                if not args.no_browser:webbrowser.open(path.resolve().as_uri())
                return path
            if args.repair_latest or args.repair_data:
                source=args.repair_data or latest_study(base)
                print('使用原数据：'+str(source.resolve()),flush=True)
                dataset=repair_market_data(api,load_study(source),output,source.parent.name)
            else:
                try:
                    dataset=collect(api,args.count,args.years,args.seed,output,daily_source=args.daily_source)
                except DataReadError as error:
                    if not error.diagnostics.get('preflightBlocked'):raise
                    path=output/'接口诊断.html'
                    print('\n'+str(error)+'\n诊断路径：'+str(path.resolve()),flush=True)
                    if not args.no_browser:webbrowser.open(path.resolve().as_uri())
                    return path
        finally:api.stop()
    path=output/'报告.html';path.write_text(report_html(dataset),encoding='utf-8')
    print('\n报告已保存：'+str(path.resolve()),flush=True)
    print('网页打开后自动计算。可点击“保存结果JSON”和“保存逐笔交易CSV”；历史原始资料仅保留本机，不自动上传。',flush=True)
    if not args.no_browser:webbrowser.open(path.resolve().as_uri())
    return path


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('回测未完成：'+(str(error) if isinstance(error,(ValueError,FileNotFoundError,ModuleNotFoundError)) else type(error).__name__),flush=True)
        sys.exit(1)
