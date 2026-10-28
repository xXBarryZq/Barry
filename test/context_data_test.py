import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from context_data import HEADERS,read_context,stamp
from random_backtest import main,save_json


class ContextDataTests(unittest.TestCase):
    def write(self,directory,filename,rows):
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=HEADERS[filename].split(','));writer.writeheader();writer.writerows(rows)
        (Path(directory)/filename).write_text(out.getvalue(),encoding='utf-8-sig')

    def macro(self,**change):
        return dict(indicator='cn_funding',observation_date='2026-10-27',available_at='2026-10-27T18:00:00+08:00',
          value='1.6',reference_value='1.5',unit='percent',vintage='first_release',source='SYNTHETIC',reference='TEST ONLY',**change)

    def test_empty_templates_are_local_pending_data_not_fake_history(self):
        with tempfile.TemporaryDirectory() as d:
            result=read_context(d)
            self.assertEqual(result['macro'],[]);self.assertEqual(result['events'],[]);self.assertEqual(result['coverage'],[])
            self.assertEqual(result['issues'],[]);self.assertEqual(len(result['files']),3)
            self.assertTrue((Path(d)/'填写说明.txt').exists())

    def test_units_first_release_timestamps_and_duplicate_periods(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d,'macro.csv',[self.macro()]);result=read_context(d)
            self.assertEqual(result['issues'],[]);self.assertEqual(result['macro'][0]['referenceValue'],1.5)
            self.assertEqual(result['macro'][0]['availableAt'],stamp('2026-10-27T10:00:00Z'))
            self.write(d,'macro.csv',[self.macro(),self.macro()]);self.assertRegex(read_context(d)['issues'][0]['message'],'重复')
            for key,value in [('unit','bp'),('vintage','revised'),('available_at','2026-10-27'),('value','nan'),
                ('value','inf'),('observation_date','2026-10-28')]:
                row=self.macro();row[key]=value;self.write(d,'macro.csv',[row]);result=read_context(d)
                self.assertTrue(result['issues'],(key,value));self.assertEqual(result['issues'][0]['module'],'macro')

    def test_pmi_requires_month_end_and_accepts_explicit_utc(self):
        with tempfile.TemporaryDirectory() as d:
            row=self.macro();row.update(indicator='cn_pmi',observation_date='2026-09-30',value='49.8',reference_value='',unit='index',available_at='2026-09-30T01:30:00Z')
            self.write(d,'macro.csv',[row]);self.assertEqual(read_context(d)['issues'],[])
            row['observation_date']='2026-09-15';self.write(d,'macro.csv',[row]);self.assertRegex(read_context(d)['issues'][0]['message'],'最后一天')

    def test_event_assessment_time_scopes_and_bounded_windows(self):
        with tempfile.TemporaryDirectory() as d:
            row=dict(id='example',scope='002466.SZ',event_type='policy',action='information',
              available_at='2026-10-27T09:00:00+08:00',assessed_at='2026-10-28T09:00:00+08:00',
              start_at='2026-10-27T09:00:00+08:00',end_at='2026-10-29T09:00:00+08:00',source='SYNTHETIC',reference='TEST ONLY',note='')
            self.write(d,'events.csv',[row]);result=read_context(d)
            self.assertEqual(result['issues'],[]);self.assertGreater(result['events'][0]['assessedAt'],result['events'][0]['availableAt'])
            for key,value in [('assessed_at','2026-10-26T09:00:00+08:00'),('scope','unknown sector'),('action','buy'),('end_at','2026-12-29T09:00:00+08:00')]:
                bad={**row,key:value};self.write(d,'events.csv',[bad]);self.assertEqual(read_context(d)['issues'][0]['module'],'events')

    def test_invalid_coverage_or_csv_is_visible_and_blocks_event_module(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d,'coverage.csv',[dict(scope='CN',start_at='2026-10-28T00:00:00+08:00',end_at='2026-10-27T00:00:00+08:00',source='TEST',reference='TEST')])
            self.assertEqual(read_context(d)['issues'][0]['module'],'events')
            (Path(d)/'coverage.csv').write_text('wrong,header\n',encoding='utf-8');self.assertRegex(read_context(d)['issues'][0]['message'],'表头')

    def test_unclosed_quotes_cannot_swallow_later_risk_rows(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'events.csv').write_text(HEADERS['events.csv']+'\n"unterminated\n',encoding='utf-8')
            result=read_context(d)
            self.assertEqual(result['issues'][0]['module'],'events');self.assertEqual(result['events'],[])

    def test_offline_context_cli_reuses_stock_prices_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.json';output=root/'new'
            data={'schema':'barry-random-study-v1','plan':{'startDate':'2026-10-01','endDate':'2026-10-27'},
              'stocks':[{'code':'002466.SZ','hourlyBars':[{'sample':'unchanged'}],'dailyBars':[]}],'universe':{}}
            save_json(source,data);before=source.read_bytes()
            page=main(['--from-json',str(source),'--context-dir',str(root/'inputs'),'--output',str(output),'--no-browser'])
            after=json.loads((output/'回测数据.json').read_text())
            self.assertEqual(after['stocks'],data['stocks']);self.assertEqual(after['plan'],data['plan']);self.assertEqual(before,source.read_bytes())
            self.assertTrue(page.exists());self.assertIn('cn-macro-events-v1',page.read_text());self.assertNotIn('import {',page.read_text())


if __name__=='__main__':unittest.main()
