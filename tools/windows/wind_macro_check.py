"""Read-only EDB diagnostics: no price queries, ledger writes, or rule changes."""
import argparse
from datetime import datetime,timedelta,date
import html
import json
import os
from pathlib import Path
import sys
import time
import webbrowser

from wind_data import CHINA
from wind_macro import config_value,fetch_macro,SERIES


def probe_macro(api,config,begin,end):
    last=date.fromisoformat(end);recent=(last-timedelta(days=120)).isoformat()
    checks=[]
    for scope,start,options in [('recent_native',recent,''),('recent_fill_previous',recent,'Fill=Previous'),('study_native',begin,'')]:
        batch=fetch_macro(api,config,start,end,options)
        for item in batch['series']:
            checks.append({k:v for k,v in item.items() if k not in ('points','max')}|
              {'scope':scope,'begin':start,'end':end,'validRows':len(item['points']),
               'lastValidSamples':item['points'][-3:]})
    conclusions=[]
    for key,meta in SERIES.items():
        rows={c['scope']:c for c in checks if c['key']==key}
        raw,filled,long=rows['recent_native'],rows['recent_fill_previous'],rows['study_native']
        if raw['validRows'] and not long['validRows']:
            finding='近期原始查询可读，长区间不可读；需继续核对历史范围与查询限制。'
        elif not raw['validRows'] and filled['validRows']:
            finding='仅Fill=Previous返回有效值；需核对填充来源与观察期，暂不自动采用。'
        elif any(c['validRows'] for c in rows.values()):
            finding='至少一种原始查询返回有效值；历史首发时间与指标身份仍需核验。'
        elif any(c.get('errorCode')==-40522015 for c in rows.values()) or (
            all(not c['validRows'] for c in rows.values()) and any(c.get('errorCode') in (None,0) for c in rows.values())):
            finding='未见有效数值（无权限错误码或 CLOSE 全空）。Wind客服确认常因 API 流量配额超限，按168小时滚动恢复；请暂缓重复诊断与长区间拉取。'
        elif any(c.get('errorCode') not in (None,0) for c in rows.values()):
            finding='接口仍返回错误码；具体是指标、授权或服务问题，需由Wind确认，程序不猜测错误含义。'
        else:
            finding='没有有效数值；请看原始字段、空值/非数类型及样例，尚不能归因于权限。'
        conclusions.append({'key':key,'name':meta['name'],'finding':finding})
    return {'schema':'barry-wind-macro-diagnostics-v1','checkedAt':datetime.now(CHINA).isoformat(),
      'stockQueries':0,'ledgerWrites':False,'checks':checks,'conclusions':conclusions,
      'note':'只比较近期/长区间及原始/填充查询，不修改宏观留档或模型。完整诊断最多9次EDB请求，会消耗API流量配额（168小时滚动恢复）。填充值不证明历史首次发布时间，也不代表策略验证通过。'}


def diagnostic_html(report):
    def esc(value):return html.escape(str(value if value is not None else '—'))
    labels={'recent_native':'近期原始','recent_fill_previous':'近期Fill=Previous','study_native':'原研究长区间'}
    rows=''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in
      [c['name'],c['code'],labels[c['scope']],c['begin']+' 至 '+c['end'],c.get('errorCode'),
       c.get('diagnostics',{}).get('rows',0),c['validRows'],c.get('error') or '返回有效数值'])+'</tr>' for c in report.get('checks',[]))
    conclusions=''.join('<p><strong>'+esc(c['name'])+'：</strong>'+esc(c['finding'])+'</p>' for c in report.get('conclusions',[]))
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
      '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
      '<title>Wind宏观接口诊断</title><style>body{font:16px/1.7 system-ui;max-width:1200px;margin:24px auto;padding:0 20px;color:#18242d}table{border-collapse:collapse;display:block;overflow:auto}td,th{border:1px solid #ccd3da;padding:8px;white-space:nowrap}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f6;padding:16px}</style>'
      '<h1>Wind宏观接口诊断</h1><p>'+esc(report['note'])+'</p>'+conclusions+
      '<table><thead><tr>'+''.join('<th>'+s+'</th>' for s in ['指标','编号','查询','区间','错误码','返回行','有效数值','结果'])+'</tr></thead><tbody>'+rows+'</tbody></table>'
      '<p>请提供同文件夹中的“宏观接口诊断.json”，无需重跑股票回测，无需提供账号密码。</p><details><summary>字段、类型与样例</summary><pre>'+esc(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></details></html>')


def main(argv=None,worker=None):
    parser=argparse.ArgumentParser(description='检查Wind宏观原始返回，不重新查询股票')
    parser.add_argument('--output',type=Path);parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    if sys.platform!='win32' and worker is None:raise ValueError('请在已登录Wind的Windows电脑运行')
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'BarryMarketWatch'
    config_path=base/'wind-macro.json'
    config=config_value(json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {})
    end=datetime.now(CHINA).date().isoformat();begin=(date.fromisoformat(end)-timedelta(days=1495)).isoformat()
    try:
        from random_backtest import latest_study,load_study
        data=load_study(latest_study(base/'backtests'))
        begin=(date.fromisoformat(data['plan']['startDate'])-timedelta(days=400)).isoformat()
    except (ValueError,OSError):pass
    output=args.output or base/'macro-diagnostics'/datetime.now(CHINA).strftime('run-%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True,exist_ok=True)
    from history_worker import HistoryWorker
    worker=worker or HistoryWorker(timeout=90)
    print('注意：完整诊断会对每个指标发起近期原始、Fill=Previous、长区间共最多9次 EDB 请求，消耗 Wind API 流量配额（168小时滚动恢复）。配额不足时请勿重复运行。',flush=True)
    print('正在检查3个宏观指标，每项比较近期原始、近期填充及长区间读取；不查询股票。',flush=True)
    try:
        if not worker.submit({'kind':'macro_diagnose','config':config,'begin':begin,'end':end}):raise ValueError('检查进程正忙')
        while True:
            result=worker.poll()
            if result:
                if 'error' in result:
                    report={'schema':'barry-wind-macro-diagnostics-v1','checkedAt':datetime.now(CHINA).isoformat(),
                      'checks':[],'conclusions':[],'stockQueries':0,'ledgerWrites':False,
                      'note':'检查尚未完成：'+result['error']+'。不能据此判断没有数据权限。'}
                else:report=result['data']
                break
            time.sleep(.1)
    finally:worker.close()
    from random_backtest import save_json
    save_json(output/'宏观接口诊断.json',report)
    page=output/'宏观接口诊断.html';page.write_text(diagnostic_html(report),encoding='utf-8')
    print('检查文件：'+str((output/'宏观接口诊断.json').resolve()),flush=True)
    if not args.no_browser:
        webbrowser.open(page.resolve().as_uri())
        if sys.platform=='win32':
            try:os.startfile(output)
            except OSError:pass
    return page


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('宏观检查未完成：'+(str(error) if isinstance(error,(ValueError,OSError)) else type(error).__name__),flush=True)
        sys.exit(1)
