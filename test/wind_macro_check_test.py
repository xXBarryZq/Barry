from datetime import datetime
from pathlib import Path
import json,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from wind_macro import config_value,fetch_macro,response_diagnostics,MacroStore
from wind_macro_check import probe_macro,main,diagnostic_html
from wind_macro_test import API


class MacroDiagnosticsTest(unittest.TestCase):
    def test_types_distinguish_vendor_null_nonfinite_and_unparsed_numeric_text_without_secrets(self):
        response=SimpleNamespace(ErrorCode=0,Fields=['CLOSE'],Codes=['M0017126'],Times=[datetime(2026,9,30)],
          Data=[[None,float('nan'),float('inf'),50.2,'49.8%','do-not-export-secret']])
        value=response_diagnostics(response);column=value['columns'][0]
        self.assertEqual(column['types'],{'none':1,'nonfinite':2,'finite_number':1,'other_text':2})
        self.assertIn({'numericText':'49.8%'},column['lastSamples'])
        serialized=json.dumps(value,allow_nan=False)
        self.assertNotIn('do-not-export-secret',serialized);self.assertNotIn('NaN',serialized)

    def test_probes_nine_edb_queries_and_never_labels_filling_as_a_collector_fix(self):
        class FilledOnly(API):
            def edb(self,code,begin,end,options):
                r=super().edb(code,begin,end,options)
                if options=='':r.Data=[[None]*len(r.Times)]
                return r
        api=FilledOnly();result=probe_macro(api,config_value({}),'2022-09-25','2026-10-28')
        self.assertEqual(len(api.calls),9);self.assertEqual(len(result['checks']),9)
        self.assertTrue(all('仅Fill=Previous' in c['finding'] for c in result['conclusions']))
        self.assertEqual(result['stockQueries'],0);self.assertFalse(result['ledgerWrites'])
        self.assertTrue(all('points' not in c for c in result['checks']))
        filled=fetch_macro(api,config_value({}),'2026-06-30','2026-10-28','Fill=Previous')
        store=MacroStore()
        with self.assertRaisesRegex(ValueError,'诊断填充'):store.append(filled)
        self.assertFalse(store.context()['macro'])

    def test_recent_success_and_long_failure_are_compared_separately(self):
        class RecentOnly(API):
            def edb(self,code,begin,end,options):
                r=super().edb(code,begin,end,options)
                if begin<'2026-01-01':r.Data=[[None]*len(r.Times)]
                return r
        result=probe_macro(RecentOnly(),config_value({}),'2022-09-25','2026-10-28')
        self.assertTrue(all('近期原始查询可读，长区间不可读' in c['finding'] for c in result['conclusions']))

    def test_report_keeps_exact_error_codes_without_guessing_their_meaning(self):
        api=API();api.fail='M0041502';api.empty='M0017126'
        result=probe_macro(api,config_value({}),'2022-09-25','2026-10-28')
        self.assertTrue(all(c['errorCode']==-7 for c in result['checks'] if c['key']=='dr007'))
        self.assertIn('程序不猜测',next(c['finding'] for c in result['conclusions'] if c['key']=='dr007'))
        self.assertTrue(all(c['diagnostics']['columns'][0]['types']=={'none':2} for c in result['checks'] if c['key']=='pmi'))

    def test_cli_only_writes_new_diagnostic_files_and_preserves_ledger_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'BarryMarketWatch';base.mkdir()
            config=base/'wind-macro.json';config.write_text(json.dumps(config_value({})))
            ledger=base/'wind-macro.sqlite3';ledger.write_bytes(b'do not touch this existing archive')
            before_config=config.read_bytes();before_ledger=ledger.read_bytes()
            class Worker:
                closed=False
                def submit(self,job):self.job=job;return True
                def poll(self):
                    return {'data':probe_macro(API(),self.job['config'],'2022-09-25','2026-10-28')}
                def close(self):self.closed=True
            worker=Worker()
            with patch.dict('os.environ',{'LOCALAPPDATA':folder}):
                page=main(['--output',str(root/'diagnostic'),'--no-browser'],worker=worker)
            report=json.loads((page.parent/'宏观接口诊断.json').read_text())
            self.assertEqual(report['stockQueries'],0);self.assertTrue(worker.closed)
            self.assertEqual(config.read_bytes(),before_config);self.assertEqual(ledger.read_bytes(),before_ledger)
            self.assertNotIn('first_release',report['note'])
            self.assertTrue(page.exists());self.assertFalse((base/'backtests').exists())

    def test_timeout_generates_a_reviewable_report_and_html_escapes_text(self):
        class Worker:
            def submit(self,job):return True
            def poll(self):return {'error':'test timeout <script>alert(1)</script>'}
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'LOCALAPPDATA':folder}):
            page=main(['--output',str(Path(folder)/'out'),'--no-browser'],worker=Worker())
            text=page.read_text();self.assertIn('检查尚未完成',text);self.assertNotIn('<script>',text)


if __name__=='__main__':unittest.main()
