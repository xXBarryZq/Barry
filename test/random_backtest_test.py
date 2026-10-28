from datetime import date,datetime,time,timedelta
import json
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from random_backtest import collect,diagnose,fetch_stock,fetch_benchmark,history_universe,report_html,repair_market_data,latest_study,save_json,DataReadError,SLOTS


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
        self.empty_adjusted=False;self.empty_raw=False;self.clock_seconds=0;self.zero_volume=False
        self.clip_before=None;self.empty_adjusted_hours=False
        self.unsupported_index=False

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
        dates=weekdays(begin,end);times=[date.fromisoformat(d) for d in dates]
        if fields=='close':return response(['CLOSE'],times,[[None if self.empty_raw else 100]*len(dates)])
        if self.empty_adjusted:return response(['OPEN','HIGH','LOW','CLOSE','VOLUME'],times,[[None]*len(dates) for _ in range(5)])
        factors=[2 if d<'2026-01-01' and options=='PriceAdj=F' else 1 for d in dates]
        data=[[value*f for f in factors] for value in [100,102,98,100]]+[[1000]*len(dates)]
        return response(['OPEN','HIGH','LOW','CLOSE','VOLUME'],times,data)

    def wsi(self,code,fields,begin,end,options):
        self.calls.append(('wsi',code,begin,end,options))
        if self.unsupported_index and code=='000985.CSI':return response(['OUTMESSAGE'],[datetime(2026,10,28)],[['test unsupported index']],code=-40520007)
        times=[];values=[]
        for day in weekdays(begin[:10],end[:10]):
            if self.clip_before and day<self.clip_before:continue
            for slot in SLOTS:
                if (day,slot)==self.missing:continue
                clock=datetime.combine(date.fromisoformat(day),time.fromisoformat('10:00' if self.bad_slot else slot))
                clock=clock.replace(second=self.clock_seconds)
                times.append(clock)
                close=100 if slot=='15:00' else 101
                if self.bad_adjustment:close=101
                value=[100,100,100,100,1000] if (day,slot)==self.flat else [100,102,98,close,0 if self.zero_volume else 1000]
                if 'PriceAdj=F' in options or 'PriceAdj=B' in options:
                    factor=(2 if day<'2026-01-01' else 1)*(3 if 'PriceAdj=B' in options else 1)
                    value=[v*factor for v in value[:4]]+value[4:]
                    if self.empty_adjusted_hours:value=[None]*5
                values.append(value)
        data=[list(v) for v in zip(*values)] if values else [[] for _ in range(5)]
        return response(['open','high','low','close','volume'],times,data)


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

    def test_error_zero_daily_all_none_has_precise_stage_diagnostics(self):
        api=FakeWind();api.empty_adjusted=True
        with self.assertRaisesRegex(DataReadError,'前复权日线') as caught:
            fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06',daily_source='wsd')
        d=caught.exception.diagnostics
        self.assertEqual(d['adjustedDaily']['errorCode'],0)
        self.assertGreater(d['adjustedDaily']['rows'],0)
        self.assertEqual(d['adjustedDaily']['values']['CLOSE']['positive'],0)
        self.assertFalse(any(c[0]=='wsi' for c in api.calls))

    def test_empty_raw_close_does_not_silently_discard_valid_hourly_history(self):
        api=FakeWind();api.empty_raw=True
        with self.assertRaisesRegex(DataReadError,'复权匹配失败') as caught:
            fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06',daily_source='wsd')
        self.assertEqual(caught.exception.diagnostics['matchingAdjustmentDates'],0)
        self.assertFalse(any(c[0]=='wsi' for c in api.calls))

    def test_filtered_source_clocks_are_reported_as_conversion_failure(self):
        api=FakeWind();api.clock_seconds=59
        with self.assertRaisesRegex(DataReadError,'时间转换接受0行') as caught:
            fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')
        self.assertGreater(caught.exception.diagnostics['conversion']['rawReturnedRows'],0)
        self.assertEqual(caught.exception.diagnostics['conversion']['parsedRows'],0)

    def test_zero_volume_is_distinguished_from_missing_price_or_permission(self):
        api=FakeWind();api.zero_volume=True
        with self.assertRaisesRegex(DataReadError,'成交量') as caught:
            fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')
        d=caught.exception.diagnostics
        self.assertGreater(d['hourlyRequests'][0]['values']['CLOSE']['positive'],0)
        self.assertEqual(d['hourlyRequests'][0]['values']['VOLUME']['positive'],0)

    def test_diagnosis_checks_only_recent_data_and_saves_no_full_vendor_history(self):
        api=FakeWind();api.empty_adjusted=True
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp);path=diagnose(api,output,today=date(2026,10,28))
            result=json.loads((output/'接口诊断.json').read_text())
            self.assertEqual(len(result['checks']),2)
            self.assertTrue(all(c['ok'] for c in result['checks']))
            self.assertTrue(all('WSI' in c['quality']['dailySource'] for c in result['checks']))
            self.assertNotIn('dailyBars',json.dumps(result))
            self.assertTrue(path.exists())
            self.assertFalse(any(c[0]=='wset' for c in api.calls))

    def test_empty_wsd_recovers_with_one_uniform_adjusted_hourly_response(self):
        api=FakeWind();api.empty_adjusted=True
        dates=weekdays('2025-01-01','2026-02-06')
        value=fetch_stock(api,{'code':'510300.SH'},dates,'2025-07-01','2026-02-06')
        self.assertIn('WSI',value['quality']['dailySource'])
        self.assertEqual(value['quality']['invalidHourly'],0)
        calls=[c for c in api.calls if c[0]=='wsi']
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][-1],'BarSize=60;PriceAdj=B')
        self.assertEqual(value['quality']['priceAdjustment'],'B')
        self.assertTrue(calls[0][2].startswith(dates[0]))
        for b in value['dailyBars']:
            self.assertEqual(b['close'],600 if b['date']<'2026-01-01' else 300)
            self.assertEqual(b['volume'],4000)
        self.assertIn('前复权日线',value['diagnostics']['fallbackReason'])

    def test_reconstructed_daily_requires_all_four_real_hourly_bars(self):
        api=FakeWind();api.empty_adjusted=True;api.missing=('2026-02-02','10:30')
        value=fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')
        missing=next(b for b in value['dailyBars'] if b['date']=='2026-02-02')
        self.assertIsNone(missing['close'])
        self.assertEqual(value['quality']['invalidDaily'],1)
        self.assertEqual(value['quality']['invalidHourly'],1)

    def test_truncated_fallback_never_changes_the_requested_research_period(self):
        api=FakeWind();api.empty_adjusted=True;api.clip_before='2026-01-01'
        with self.assertRaisesRegex(DataReadError,'研究起点'):
            fetch_stock(api,{'code':'510300.SH'},weekdays('2025-01-01','2026-02-06'),'2025-07-01','2026-02-06')

    def test_adjustment_request_failure_does_not_fall_back_to_raw_prices(self):
        api=FakeWind();api.empty_adjusted=True;api.empty_adjusted_hours=True
        with self.assertRaisesRegex(DataReadError,'未确认复权') as caught:
            fetch_stock(api,{'code':'510300.SH'},weekdays('2026-01-01','2026-02-06'),'2026-02-02','2026-02-06')
        self.assertEqual(caught.exception.diagnostics['hourlyRequests'][0]['values']['CLOSE']['positive'],0)
        self.assertTrue(all('PriceAdj=B' in c[-1] for c in api.calls if c[0]=='wsi'))

    def test_empty_benchmark_daily_can_use_complete_index_hours_with_zero_volume(self):
        api=FakeWind();api.empty_adjusted=True;api.zero_volume=True
        result=fetch_benchmark(api,weekdays('2025-10-01','2026-02-06'),'2026-02-06')
        self.assertNotIn('error',result)
        self.assertEqual(result['dailyBars'][-1]['close'],300)
        self.assertTrue(all(b['volume']==0 for b in result['dailyBars']))

    def test_unsupported_index_uses_disclosed_etf_proxy_not_a_renamed_index(self):
        api=FakeWind();api.empty_adjusted=True;api.unsupported_index=True
        result=fetch_benchmark(api,weekdays('2025-10-01','2026-02-06'),'2026-02-06')
        self.assertNotIn('error',result)
        self.assertEqual(result['code'],'510300.SH');self.assertTrue(result['isProxy'])
        self.assertEqual(result['requestedCode'],'000985.CSI')
        self.assertIn('-40520007',result['diagnostics']['originalError'])
        self.assertIn('不同',result['role'])

    def test_missing_index_and_proxy_keeps_market_filter_blocked(self):
        api=FakeWind();api.empty_adjusted=True;api.empty_adjusted_hours=True;api.unsupported_index=True
        result=fetch_benchmark(api,weekdays('2025-10-01','2026-02-06'),'2026-02-06')
        self.assertIn('error',result);self.assertEqual(result['dailyBars'],[])
        self.assertIn('-40520007',result['error'])

    def test_market_repair_reuses_every_stock_bar_and_never_overwrites_source(self):
        api=FakeWind()
        with tempfile.TemporaryDirectory() as temp:
            original=Path(temp)/'run-original';original.mkdir()
            data=collect(api,3,1,42,original,today=date(2026,10,28))
            data['benchmark']={'code':'000985.CSI','dailyBars':[],'error':'original index -40520007'}
            save_json(original/'回测数据.json',data)
            before=(original/'回测数据.json').read_bytes();snapshot=copy.deepcopy(data)
            api.calls=[];api.empty_adjusted=True;api.unsupported_index=True
            output=Path(temp)/'run-repaired';output.mkdir()
            repaired=repair_market_data(api,data,output,original.name)
            self.assertEqual(repaired['stocks'],snapshot['stocks'])
            self.assertEqual(repaired['plan'],snapshot['plan'])
            self.assertEqual(data,snapshot)
            self.assertEqual((original/'回测数据.json').read_bytes(),before)
            self.assertTrue((output/'回测数据.json').exists())
            self.assertTrue(repaired['benchmark']['isProxy'])
            self.assertTrue(repaired['repair']['stockPricesReused'])
            self.assertTrue(all(c[0]!='wset' for c in api.calls))
            self.assertTrue(all(c[1] in ('000985.CSI','510300.SH') for c in api.calls if c[0] in ('wsd','wsi')))

    def test_latest_repair_source_skips_invalid_and_failed_only_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            minimal={'schema':'barry-random-study-v1','plan':{'startDate':'2025-01-01','endDate':'2026-01-01'},
                     'stocks':[{'dailyBars':[{}],'hourlyBars':[{}]}]}
            for i,name in enumerate(['run-valid','run-failed','run-corrupt']):
                path=base/name;path.mkdir();file=path/'回测数据.json'
                if i==0:save_json(file,minimal)
                elif i==1:save_json(file,{**minimal,'stocks':[{'error':'failed'}]})
                else:file.write_text('{broken',encoding='utf8')
                os.utime(file,ns=(1000000+i,1000000+i))
            self.assertEqual(latest_study(base).parent.name,'run-valid')

    def test_preflight_failure_stops_before_querying_the_random_cohort(self):
        api=FakeWind();api.empty_adjusted=True;api.empty_adjusted_hours=True
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)
            with self.assertRaisesRegex(DataReadError,'预检') as caught:
                collect(api,3,1,42,output,today=date(2026,10,28))
            self.assertTrue(caught.exception.diagnostics['preflightBlocked'])
            self.assertTrue((output/'抽样计划.json').exists())
            self.assertTrue((output/'接口诊断.html').exists())
            self.assertFalse((output/'回测数据.json').exists())
            self.assertTrue(all(c[1] in ('510300.SH','600519.SH') for c in api.calls if c[0]=='wsi'))

    def test_empty_wsd_full_collection_recovers_fixed_samples_and_benchmark(self):
        api=FakeWind();api.empty_adjusted=True
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp);api.plan_path=output/'抽样计划.json'
            dataset=collect(api,3,3,42,output,today=date(2026,10,28))
            self.assertEqual(len(dataset['stocks']),3)
            self.assertTrue(all('error' not in s for s in dataset['stocks']))
            self.assertTrue(all('WSI' in s['quality']['dailySource'] for s in dataset['stocks']))
            self.assertNotIn('error',dataset['benchmark'])
            self.assertEqual({s['code'] for s in dataset['stocks']},{s['code'] for s in dataset['plan']['sample']})
            self.assertGreater(dataset['stocks'][0]['diagnostics']['warmupDerivedDaily'],0)

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
