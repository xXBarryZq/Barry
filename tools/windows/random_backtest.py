"""Read-only, fixed-cohort Wind research. No orders and no dashboard rule changes."""
import argparse
from datetime import date, datetime, time, timedelta
import hashlib
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

FIELDS='open,high,low,close,volume'
SLOTS=('10:30','11:30','14:00','15:00')


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


def fetch_stock(api,stock,dates,start,end):
    begin=dates[0];cutoff=date.fromisoformat(end)+timedelta(days=1)
    now=timestamp(datetime.combine(cutoff,time()),CHINA)
    adjusted=checked(api.wsd(stock['code'],FIELDS,begin,end,'PriceAdj=F'),'前复权日线 '+stock['code'])
    daily_by={b['date']:b for b in rows(adjusted,'daily',now,CHINA,cutoff.isoformat()) if begin<=b['date']<=end}
    raw=checked(api.wsd(stock['code'],'close',begin,end,''),'原始收盘价 '+stock['code'])
    raw_by={source_date(v).isoformat():finite(cells(raw,i).get('CLOSE'),positive=True) for i,v in enumerate(getattr(raw,'Times',None) or [])}
    factors={day:b['close']/raw_by[day] for day,b in daily_by.items() if b['close'] and raw_by.get(day)}
    found={};bad_slots=set()
    chunk=date.fromisoformat(start);last=date.fromisoformat(end)
    while chunk<=last:
        chunk_end=min(last,chunk+timedelta(days=179))
        print('    读取60分钟：'+chunk.isoformat()+' 至 '+chunk_end.isoformat(),flush=True)
        response=checked(api.wsi(stock['code'],FIELDS,chunk.isoformat()+' 09:30:00',chunk_end.isoformat()+' 15:00:00','BarSize=60'),'60分钟 '+stock['code'])
        for bar in rows(response,'hourly',now,CHINA):
            local=datetime.fromtimestamp(bar['time']/1000,CHINA);slot=local.strftime('%H:%M')
            if not start<=bar['date']<=end:continue
            if slot not in SLOTS:bad_slots.add(slot);continue
            factor=factors.get(bar['date'])
            if factor is None:continue
            # Fetch unadjusted chunks, then use ONE daily adjustment basis for all chunks.
            for field in ('open','high','low','close'):
                if bar[field] is not None:bar[field]*=factor
            bar['slot']=slot
            found[(bar['date'],slot)]=bar
        chunk=chunk_end+timedelta(days=1)
    if bad_slots:raise ValueError('Wind60分钟结束时刻与预设交易时段不符：'+','.join(sorted(bad_slots))+'；需确认后再回测')
    for (day,slot),bar in found.items():
        close=daily_by.get(day,{}).get('close')
        if slot=='15:00' and close and bar.get('close') and abs(bar['close']-close)>max(.02*factors[day],close*.003):
            raise ValueError('小时末价与同日日线收盘不一致（'+day+'）；需核对小时结束时间及原始/复权口径，停止该样本')
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
    if not valid_hours:raise ValueError('没有可用的完整60分钟价格；可能为历史权限、时间口径或停牌问题')
    early_days=[d for d in test_dates if d<= (date.fromisoformat(start)+timedelta(days=30)).isoformat()]
    if valid_hours[0]['date']>early_days[-1] and sum(bool(daily_by.get(d,{}).get('volume')) for d in early_days)>=5:
        raise ValueError('日线显示期初有交易，但小时数据未覆盖期初，疑似WSI历史被截断；不使用剩余区间冒充完整研究')
    daily=[daily_by.get(day) or placeholder(day) for day in dates]
    return {**stock,'dailyBars':daily,'hourlyBars':hourly,'calendar':test_dates,
        'quality':{'invalidDaily':sum(b['close'] is None or not b.get('valid',True) for b in daily),'invalidHourly':invalid,'flatHourly':flat,
                   'expectedHourly':len(hourly),'firstHourDate':valid_hours[0]['date'],'lastHourDate':valid_hours[-1]['date']}}


def save_json(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')),encoding='utf-8')
    temp.replace(path)


def engine_hash(repo=None):
    repo=Path(repo) if repo else Path(__file__).resolve().parents[2]
    return hashlib.sha256((repo/'src/swing.js').read_bytes()+b'\n'+(repo/'tools/research/evaluate.js').read_bytes()).hexdigest()


def report_html(dataset,repo=None):
    repo=Path(repo) if repo else Path(__file__).resolve().parents[2]
    source=(repo/'src/swing.js').read_text(encoding='utf-8')
    research=(repo/'tools/research/evaluate.js').read_text(encoding='utf-8')
    research=re.sub(r'^import[^\n]*\n','',research,flags=re.M)
    bundle=re.sub(r'^export\s+','',source+'\n'+research,flags=re.M)
    bundle=re.sub(r'</script',r'<\\/script',bundle,flags=re.I)
    dataset={**dataset,'evaluationEngineSha256':engine_hash(repo)}
    data=json.dumps(dataset,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
        '<title>随机A股回测报告</title><style>body{font:16px/1.7 system-ui,sans-serif;color:#18242d;background:#f7f8fa;max-width:1400px;margin:24px auto;padding:0 20px}table{border-collapse:collapse;width:100%;display:block;overflow:auto;background:white;margin:20px 0}th,td{border:1px solid #d8dee5;padding:8px 12px;white-space:nowrap;text-align:left}th{background:#e9eef3}button{margin:4px 12px 4px 0;padding:10px 16px;cursor:pointer}p{max-width:1100px}h1{font-size:26px}h2{font-size:21px}</style>'
        '<main id="study">正在计算回测结果，请稍候……</main><script type="application/json" id="data">'+data+'</script><script>'+bundle+
        '\ntry{renderStudy(JSON.parse(document.getElementById("data").textContent));}catch(error){document.getElementById("study").textContent="计算失败："+error.message;throw error;}</script></html>')


def collect(api,count,years,seed,output,today=None):
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
          'sample':selected,'commission':0,'ruleVersion':'price-only-exploration-v1','engineSha256':engine_hash()}
    save_json(output/'抽样计划.json',{'plan':plan,'universe':universe}) # Before any price query.
    print(f'已固定随机样本{count}只，不按收益挑选或替换。研究区间：{start} 至 {end}',flush=True)
    print('名单：'+', '.join(s['code'] for s in selected),flush=True)
    benchmark={'code':'000985.CSI','dailyBars':[]}
    try:
        result=checked(api.wsd(benchmark['code'],FIELDS,dates[0],end,'PriceAdj=F'),'中证全指日线')
        by={b['date']:b for b in rows(result,'daily',timestamp(datetime.combine(today,time()),CHINA),CHINA,today.isoformat())}
        benchmark['dailyBars']=[by.get(day) or placeholder(day) for day in dates]
    except ValueError as error:benchmark['error']=str(error);print('市场过滤暂不可用：'+str(error),flush=True)
    stocks=[]
    for i,stock in enumerate(selected):
        print(f'[{i+1}/{count}] '+stock['code']+' '+stock['name'],flush=True)
        try:
            value=fetch_stock(api,stock,dates,start,end)
            print('    完成；无效小时 '+str(value['quality']['invalidHourly']),flush=True)
        except KeyboardInterrupt:
            stocks.extend({**s,'error':'用户中止，未完成读取'} for s in selected[i:])
            break
        except Exception as error:
            message=str(error) if isinstance(error,ValueError) else '接口异常类型：'+type(error).__name__
            value={**stock,'error':message};print('    保留失败样本：'+message,flush=True)
        stocks.append(value);save_json(output/(stock['code']+'.json'),value)
    dataset={'schema':'barry-random-study-v1','plan':plan,'universe':universe,'benchmark':benchmark,
             'collectedAt':datetime.now(CHINA).isoformat(),'dataSource':'本机WindPy，只读WSET/TDAYS/WSD/WSI','stocks':stocks}
    save_json(output/'回测数据.json',dataset)
    return dataset


def main(argv=None):
    parser=argparse.ArgumentParser(description='固定随机抽取沪深A股，以本机Wind历史检验价量假设。不会下单。')
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--years',type=int,default=3)
    parser.add_argument('--seed',type=int,default=20261028)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--from-json',type=Path,help='从已有本地回测数据重新生成报告，不连接Wind')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.count<=100 or not 1<=args.years<=10:parser.error('样本须1—100只，年数须1—10年')
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'BarryMarketWatch' / 'backtests'
    output=args.output or base/datetime.now(CHINA).strftime('run-%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True,exist_ok=True)
    if args.from_json:
        dataset=json.loads(args.from_json.read_text(encoding='utf-8-sig'))
        if dataset.get('schema')!='barry-random-study-v1':raise ValueError('输入不是本工具的历史资料文件')
    else:
        if sys.platform!='win32':raise ValueError('真实Wind回测须在已安装WindPy的Windows电脑运行；云端无法访问本机终端')
        api=importlib.import_module('WindPy').w
        try:
            checked(api.start(),'连接Wind')
            if not api.isconnected():raise ValueError('Wind未连接，请保持终端登录')
            dataset=collect(api,args.count,args.years,args.seed,output)
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
