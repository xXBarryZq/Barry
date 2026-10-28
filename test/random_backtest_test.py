from datetime import date,datetime,time,timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from random_backtest import collect,fetch_stock,history_universe,report_html,SLOTS


def weekdays(begin,end):
    day=date.fromisoformat(begin);last=date.fromisoformat(end);result=[]
    while day<=last:
        if day.weekday()<5:result.append(day.isoformat())
        day+=timedelta(days=1)
    return result


def response(fields,times,data,code=0):
    return SimpleNamespace(ErrorCode=code,Fields=fields,Times=times,Data=data)


class FakeWind:
    def __init__(self):
        self.calls=[];self.missing=None;self.flat=None;self.bad_slot=False;self.fail_code=None;self.plan_path=None;self.mismatch=False;self.bad_adjustment=False

    def tdays(self,begin,end,options):
        return response(['date'],[],[[date.fromisoformat(d) for d in weekdays(begin,end)]])

    def wset(self,name,options):
        self.calls.append(('wset',options))
        day=options.split(';')[0].split('=')[1]
        if self.mismatch:day='2026-10-28'
        return response(['date','wind_code','sec_name'],[],[[date.fromisoformat(day)]*4,
            ['002466.SZ','600001.SH','300750.SZ','510300.SH'],['测试甲','测试乙','测试丙','ETF应排除']])

    def wsd(self,code,fields,begin,end,options):
        self.calls.append(('wsd',code,fields,begin,end,options))
        if self.plan_path is not None:assert self.plan_path.exists(),'sampling plan must exist before prices are read'
        if code==self.fail_code:return response([],[],[],code=-999)
        dates=weekdays(begin,end);times=[datetime.fromisoformat(d) for d in dates]
        if fields=='close':return response(['CLOSE'],times,[[100]*len(dates)])
        factors=[2 if d<'2026-01-01' and options=='PriceAdj=F' else 1 for d in dates]
        data=[[value*f for f in factors] for value in [100,102,98,100]]+[[1000]*len(dates)]
        return response(['OPEN','HIGH','LOW','CLOSE','VOLUME'],times,data)

    def wsi(self,code,fields,begin,end,options):
        self.calls.append(('wsi',code,begin,end,options))
        times=[];values=[]
        for day in weekdays(begin[:10],end[:10]):
            for slot in SLOTS:
                if (day,slot)==self.missing:continue
                clock=datetime.combine(date.fromisoformat(day),time.fromisoformat('10:00' if self.bad_slot else slot))
                times.append(clock)
                close=100 if slot=='15:00' else 101
                if self.bad_adjustment:close=101
                values.append([100,100,100,100,1000] if (day,slot)==self.flat else [100,102,98,close,1000])
        data=[list(v) for v in zip(*values)] if values else [[] for _ in range(5)]
        return response(['OPEN','HIGH','LOW','CLOSE','VOLUME'],times,data)


class RandomBacktestTests(unittest.TestCase):
    def test_seed_is_reproducible_and_etfs_excluded(self):
        api=FakeWind()
        a,meta=history_universe(api,'2023-10-30',3,42)
        b,_=history_universe(api,'2023-10-30',3,42)
        self.assertEqual(a,b);self.assertEqual(meta['count'],3)
        self.assertTrue(meta['dateFieldVerified'])
        self.assertNotIn('510300.SH',[s['code'] for s in a])

    def test_wrong_historical_universe_date_aborts(self):
        api=FakeWind();api.mismatch=True
        with self.assertRaisesRegex(ValueError,'日期'):history_universe(api,'2023-10-30',2,42)
        self.assertFalse(any(c[0]=='wsd' for c in api.calls))

    def test_raw_hour_chunks_use_one_daily_adjustment_basis(self):
        api=FakeWind();dates=weekdays('2025-01-01','2026-02-06')
        stock=fetch_stock(api,{'code':'002466.SZ','name':'测试'},dates,'2025-01-01','2026-02-06')
        for b in stock['hourlyBars']:
            original=100 if b['slot']=='15:00' else 101
            expected=original*(2 if b['date']<'2026-01-01' else 1)
            self.assertEqual(b['close'],expected)
        calls=[c for c in api.calls if c[0]=='wsi']
        self.assertGreater(len(calls),1)
        self.assertTrue(all(c[-1]=='BarSize=60' for c in calls))

    def test_missing_and_flat_hours_never_become_filled_prices(self):
        api=FakeWind();api.missing=('2026-02-02','10:30');api.flat=('2026-02-03','11:30')
        dates=weekdays('2026-01-01','2026-02-06')
        stock=fetch_stock(api,{'code':'002466.SZ'},dates,'2026-02-02','2026-02-06')
        self.assertEqual(stock['quality']['flatHourly'],1)
        self.assertEqual(stock['quality']['invalidHourly'],2)
        for key in [api.missing,api.flat]:
            b=next(b for b in stock['hourlyBars'] if (b['date'],b['slot'])==key)
            self.assertIsNone(b['close'])

    def test_unconfirmed_hour_timestamp_convention_is_rejected(self):
        api=FakeWind();api.bad_slot=True
        with self.assertRaisesRegex(ValueError,'结束时刻'):
            fetch_stock(api,{'code':'002466.SZ'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')

    def test_inconsistent_daily_and_hourly_adjustment_is_rejected(self):
        api=FakeWind();api.bad_adjustment=True
        with self.assertRaisesRegex(ValueError,'复权口径'):
            fetch_stock(api,{'code':'002466.SZ'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')

    def test_failed_sample_is_not_replaced_and_plan_precedes_prices(self):
        api=FakeWind();api.fail_code='002466.SZ'
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp);api.plan_path=output/'抽样计划.json'
            dataset=collect(api,3,1,42,output,today=date(2026,10,28))
            self.assertEqual(len(dataset['stocks']),3)
            failed=[s for s in dataset['stocks'] if s.get('error')]
            self.assertEqual(len(failed),1);self.assertEqual(failed[0]['code'],'002466.SZ')
            self.assertIn('-999',failed[0]['error'])
            self.assertLess(dataset['plan']['endDate'],'2026-10-28')
            saved=json.loads((output/'抽样计划.json').read_text())
            self.assertEqual(saved['plan']['sample'],dataset['plan']['sample'])

    def test_single_file_report_escapes_embedded_security_text(self):
        value={'schema':'barry-random-study-v1','stocks':[{'name':'</script><script>alert(1)</script>&'}]}
        page=report_html(value)
        self.assertNotIn('</script><script>alert(1)',page)
        self.assertIn('\\u003c/script',page)
        self.assertNotIn("import {PROFILES",page)
        self.assertNotIn('export function',page)
        self.assertEqual(page.count('const researchEma='),1)
        self.assertIn('default-src',page)


if __name__=='__main__':unittest.main()
