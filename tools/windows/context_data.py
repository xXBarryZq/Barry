"""Local, explicit, point-in-time research imports. Never fetch or infer news."""
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import re

HEADERS={
    'macro.csv':'indicator,observation_date,available_at,value,reference_value,unit,vintage,source,reference',
    'events.csv':'id,scope,event_type,action,available_at,assessed_at,start_at,end_at,source,reference,note',
    'coverage.csv':'scope,start_at,end_at,source,reference',
}
GUIDE='''宏观与事件研究资料（仅保存在本机）

这些CSV目前是空模板，不是已经接通的Wind/Bloomberg新闻或宏观接口。
不要填账号密码，不要用今天的数值回填历史；未填写时增强方案显示“未评估”。
请参阅下载包 docs/宏观与事件模型.md 的字段说明，或将供应商导出文件的字段名称交给开发者匹配。

macro.csv：cn_funding 为DR007及同日适用的7天逆回购操作利率，单位percent；
cn_pmi 为国家统计局制造业PMI首次公布值，单位index。每个指标每个观察期只接受一个首次版本。
available_at必须是实际首次可获得时间，含时区，如2026-10-28T09:30:00+08:00；不接受只有日期。
vintage填写first_release。PMI的observation_date为对应统计月份最后一天。

events.csv：只记录可核实的事件。assessed_at为这项影响分类实际作出的时间；
今天补作的主观分类不能用于过去的交易。支持information（资料）与pause_new_entries（暂停新买入）。
scope为CN（A股市场）或完整证券代码；不支持未经历史核验的行业映射。
coverage.csv：明确声明已核查的事件档案范围。市场和每只股票分别声明；空事件表不表示没有风险。
覆盖声明是导入者提供的资料范围说明，程序不能独立证实供应商的完整性。

保存CSV后，再双击“完善模型并复用最近回测.cmd”。无需重新读取20只股票，也不会改动原报告。
'''


def stamp(value):
    try:
        parsed=datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
    except ValueError as error:raise ValueError('时间必须是含时区的完整ISO日期时间') from error
    if parsed.tzinfo is None:raise ValueError('时间缺少时区，不接受只有日期或本地模糊时间')
    return round(parsed.timestamp()*1000)


def text(row,key,limit=500):
    value=(row.get(key) or '').strip()
    if not value:raise ValueError('缺少'+key)
    if len(value)>limit:raise ValueError(key+'过长')
    return value


def scope(row):
    value=text(row,'scope').upper()
    if value!='CN' and not re.fullmatch(r'\d{6}\.(SH|SZ)',value):raise ValueError('scope须为CN或沪深完整证券代码')
    return value


def number(row,key,low,high):
    try:value=float(text(row,key))
    except ValueError as error:raise ValueError(key+'必须是数值') from error
    if not low<=value<=high:raise ValueError(key+'超出允许范围，请检查单位')
    return value


def macro_row(row):
    indicator=text(row,'indicator');day=date.fromisoformat(text(row,'observation_date'))
    published=stamp(text(row,'available_at'))
    if indicator not in ('cn_funding','cn_pmi'):raise ValueError('indicator仅支持cn_funding或cn_pmi')
    expected='percent' if indicator=='cn_funding' else 'index'
    if text(row,'unit')!=expected:raise ValueError('unit须为'+expected)
    if text(row,'vintage')!='first_release':raise ValueError('仅接受首次公布版本first_release')
    available=datetime.fromisoformat(text(row,'available_at').replace('Z','+00:00'))
    # Compare dates in China, including inputs explicitly expressed in UTC.
    if day>available.astimezone(timezone(timedelta(hours=8))).date():raise ValueError('观察期晚于公布时间')
    if indicator=='cn_pmi' and (day+timedelta(days=1)).month==day.month:raise ValueError('PMI观察日期须为统计月份最后一天')
    return {'indicator':indicator,'observationDate':day.isoformat(),'availableAt':published,
      'value':number(row,'value',0,50 if indicator=='cn_funding' else 100),
      'referenceValue':number(row,'reference_value',0,50) if indicator=='cn_funding' else None,
      'source':text(row,'source',100),'reference':text(row,'reference'), 'vintage':'first_release'}


def event_row(row):
    kind=text(row,'event_type');action=text(row,'action')
    if kind not in ('scheduled_macro','scheduled_earnings','company_risk','market_risk','policy','geopolitics'):
        raise ValueError('不支持的event_type')
    if action not in ('information','pause_new_entries'):raise ValueError('不支持的action')
    pub,assessed,start,end=(stamp(text(row,k)) for k in ('available_at','assessed_at','start_at','end_at'))
    if assessed<pub:raise ValueError('影响分类时间不能早于消息首次可获得时间')
    if not 0<end-start<=30*86400000:raise ValueError('事件作用窗口须大于0且不超过30天')
    return {'id':text(row,'id',120),'scope':scope(row),'type':kind,'action':action,'availableAt':pub,
      'assessedAt':assessed,'startAt':start,'endAt':end,'source':text(row,'source',100),
      'reference':text(row,'reference'),'note':(row.get('note') or '').strip()[:240]}


def coverage_row(row):
    start,end=(stamp(text(row,k)) for k in ('start_at','end_at'))
    if end<=start:raise ValueError('覆盖结束时间须晚于开始时间')
    return {'scope':scope(row),'startAt':start,'endAt':end,'source':text(row,'source',100),'reference':text(row,'reference')}


def read_context(directory):
    """Invalid rows block their module, rather than disappearing from a backtest."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    guide=directory/'填写说明.txt'
    if not guide.exists():guide.write_text(GUIDE,encoding='utf-8-sig')
    result={'schema':'barry-context-v1','macro':[],'events':[],'coverage':[],'issues':[],'files':[],
      'importerSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'scope':'仅A股宏观与事件过滤实验；覆盖声明和历史版本需供应商核验，未接通自动新闻流。'}
    for filename,header in HEADERS.items():
        path=directory/filename
        if not path.exists():path.write_text(header+'\n',encoding='utf-8-sig')
        module='macro' if filename=='macro.csv' else 'events'
        target=filename.split('.')[0]
        try:
            if path.stat().st_size>16*1024*1024:raise ValueError('文件超过16MiB，请先分批整理')
            raw=path.read_bytes();reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')),strict=True)
            if reader.fieldnames!=header.split(','):raise ValueError('CSV表头或顺序不匹配，请保留模板表头')
            result['files'].append({'name':filename,'sha256':hashlib.sha256(raw).hexdigest()})
            seen=set()
            for line,row in enumerate(reader,2):
                if not any(str(v or '').strip() for v in row.values()):continue
                try:
                    if None in row:raise ValueError('列数超过表头；含逗号的文字需用双引号包围')
                    item={'macro':macro_row,'events':event_row,'coverage':coverage_row}[target](row)
                    key=(item['indicator'],item['observationDate']) if target=='macro' else item['id'] if target=='events' else (item['scope'],item['startAt'],item['endAt'])
                    if key in seen:raise ValueError('重复记录；不能合并多个来源或修订值')
                    seen.add(key);result[target].append(item)
                except (ValueError,TypeError) as error:
                    result['issues'].append({'module':module,'file':filename,'line':line,'message':str(error)})
        except (ValueError,OSError,UnicodeError,csv.Error) as error:
            result['issues'].append({'module':module,'file':filename,'message':str(error)})
    return result
