from datetime import datetime
from pathlib import Path
import copy,json,sys,tempfile,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from wind_macro import DEFAULT_CODES,MacroStore,binding_hash,config_value,edb_code,fetch_macro,observed_context
from wind_data import CHINA
from watchlist import WindDashboard
from wind_dashboard import make_server
from random_backtest import main,save_json

NOW=int(datetime(2026,10,28,16,tzinfo=CHINA).timestamp()*1000)
class API:
    def __init__(self):self.calls=[];self.fail=None;self.empty=None;self.mismatch=None
    def edb(self,code,begin,end,options):
        self.calls.append((code,begin,end,options))
        days,values=(['2026-08-31','2026-09-30'],[50.2,50.3]) if code==DEFAULT_CODES['pmi'] else (['2026-10-26','2026-10-27'],[1.6,1.7] if code==DEFAULT_CODES['dr007'] else [1.5,1.5])
        return SimpleNamespace(ErrorCode=-7 if code==self.fail else 0,Codes=['M9999999' if code==self.mismatch else code],
          Times=[datetime.fromisoformat(d) for d in days],Data=[[None]*len(values) if code==self.empty else values])

def batch():
    b=fetch_macro(API(),config_value({}),'2026-08-01','2026-10-28');b['readAt']=NOW;return b

class WindMacroTest(unittest.TestCase):
    def test_user_codes_preconfigured_and_query_text_is_never_executed(self):
        self.assertEqual(config_value({})['codes'],DEFAULT_CODES)
        self.assertEqual(edb_code('pmi = w.edb("M0017126", "2005-01-01", "2026-10-28", "Fill=Previous")'),'M0017126')
        self.assertEqual(edb_code('w.edb("M0017126", run_other_code())'),'M0017126')
        for bad in ['M1;other','M0017126,M0041502','__import__("os")','w.edb("M0017126,M0041502", "", "")']:
            with self.assertRaises(ValueError):edb_code(bad)
        with self.assertRaises(ValueError):config_value({'enabled':True,'codes':{'pmi':'M0017126'}})
        with self.assertRaises(ValueError):config_value({'codes':dict.fromkeys(DEFAULT_CODES,'M0017126')})

    def test_edb_history_has_explicit_observation_periods_without_invented_release_times(self):
        api=API();b=fetch_macro(api,config_value({}),'2026-08-01','2026-10-28')
        self.assertEqual(len(api.calls),3);self.assertTrue(all(c[-1]=='' for c in api.calls))
        self.assertFalse(b['historicalReleaseTimesVerified']);self.assertTrue(all(len(s['points'])==2 for s in b['series']))
        self.assertNotIn('availableAt',b['series'][0]['points'][0])

    def test_partial_errors_empty_values_and_wrong_codes_are_reported_per_indicator(self):
        api=API();api.fail=DEFAULT_CODES['dr007'];api.empty=DEFAULT_CODES['pmi'];api.mismatch=DEFAULT_CODES['omo7d']
        b=fetch_macro(api,config_value({}),'2026-08-01','2026-10-28')
        self.assertTrue(all(s.get('error') and not s['points'] for s in b['series']))
        self.assertIn('-7',b['series'][1]['error']);self.assertIn('没有有效',b['series'][0]['error'])
        self.assertIn('168小时',b['series'][0]['error'])

    def test_authority_error_mentions_rolling_quota_recovery(self):
        class Authority(API):
            def edb(self,code,begin,end,options):
                return SimpleNamespace(ErrorCode=-40522015,Codes=['ErrorReport'],Fields=['OUTMESSAGE'],
                  Times=[datetime.fromisoformat('2026-10-27')],Data=[['CWSDService:: no authority.']])
        b=fetch_macro(Authority(),config_value({}),'2026-10-20','2026-10-28')
        self.assertTrue(all('-40522015' in s['error'] and '168小时' in s['error'] for s in b['series']))
        self.assertTrue(all(s.get('outMessage','').startswith('CWSDService') for s in b['series']))

    def test_scheduler_uses_short_refresh_lookback_to_limit_quota(self):
        from wind_macro import DEFAULT_REFRESH_LOOKBACK_DAYS
        from datetime import date,timedelta
        self.assertEqual(DEFAULT_REFRESH_LOOKBACK_DAYS,35)
        s=MacroStore();s.configure({'enabled':True});jobs=[]
        class Worker:
            def poll(self):return None
            def submit(self,job):jobs.append(job);return True
        s.worker=Worker();s.tick()
        today=date.today();begin=date.fromisoformat(jobs[0]['begin'])
        self.assertEqual((today-begin).days,DEFAULT_REFRESH_LOOKBACK_DAYS)

    def test_append_only_ledger_deduplicates_and_preserves_revisions_and_old_values(self):
        s=MacroStore();b=batch();s.append(b);s.append(b)
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],6)
        changed=copy.deepcopy(b);changed['readAt']+=1000;changed['series'][0]['points'][-1]['value']=49.2;s.append(changed)
        reverted=copy.deepcopy(b);reverted['readAt']+=2000;s.append(reverted)
        rows=s.context()['macro'];pmi=[r for r in rows if r['indicator']=='cn_pmi' and r['observationDate']=='2026-09-30']
        self.assertEqual([r['value'] for r in pmi],[50.3,49.2,50.3])
        self.assertEqual([r['availableAt'] for r in pmi],[NOW,NOW+1000,NOW+2000])
        self.assertTrue(all(r['availableAt']>=NOW for r in rows));self.assertEqual(s.context()['macroSourcePolicy'],'locally_observed')
        with self.assertRaisesRegex(ValueError,'时钟'):s.append(b)

    def test_policy_rate_state_uses_only_prior_observations_and_respects_revisions(self):
        rows=[dict(id=1,key='omo7d',code='M0325686',observationDate='2026-10-25',value=1.5,seenAt=NOW),
          dict(id=2,key='dr007',code='M0041502',observationDate='2026-10-27',value=1.7,seenAt=NOW),
          dict(id=3,key='omo7d',code='M0325686',observationDate='2026-10-28',value=1.4,seenAt=NOW+1000)]
        f=observed_context(rows)['macro'];self.assertEqual(len(f),1);self.assertEqual(f[0]['referenceValue'],1.5)
        self.assertEqual(f[0]['referenceObservationDate'],'2026-10-25')
        rows.append(dict(id=4,key='omo7d',code='M0325686',observationDate='2026-10-25',value=1.6,seenAt=NOW+2000))
        f=observed_context(rows)['macro'];self.assertEqual(f[-1]['referenceValue'],1.6);self.assertEqual(f[-1]['availableAt'],NOW+2000)

    def test_binding_changes_isolate_archives_and_settings_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            s=MacroStore(directory);self.assertTrue(s.config['enabled']);s.append(batch())
            self.assertTrue(MacroStore(directory).context()['macro'])
            value={**s.config,'codes':{**s.config['codes'],'pmi':'M9999999'}};s.configure(value)
            self.assertFalse(s.context()['macro'])
            with self.assertRaisesRegex(ValueError,'配置已改变'):s.append(batch())
            self.assertEqual(MacroStore(directory).config['codes']['pmi'],'M9999999')

    def test_corrupt_macro_archive_does_not_disable_the_quote_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'wind-macro.sqlite3').write_bytes(b'corrupt test database')
            board=WindDashboard(state_path=root/'settings.json');board.set_status(True,'quotes available')
            self.assertTrue(board.snapshot()['status']['connected']);self.assertIn('留档暂不可用',board.snapshot()['macro']['message'])
            self.assertEqual((root/'wind-macro.sqlite3').read_bytes(),b'corrupt test database')

    def test_scheduler_uses_a_separate_worker_drops_old_jobs_and_contains_failures(self):
        s=MacroStore();s.configure({'enabled':True});jobs=[]
        class Worker:
            job=None;result=None
            def submit(self,job):self.job=job;jobs.append(job);return True
            def poll(self):r,self.result=self.result,None;return r
        s.worker=Worker();s.tick();self.assertTrue(s.loading);self.assertEqual(jobs[0]['kind'],'macro')
        s.worker.result={'job':{'binding':binding_hash(s.config)},'data':batch()};s.tick()
        self.assertFalse(s.loading);self.assertTrue(s.snapshot()['readAt']);self.assertEqual(len(jobs),1)
        s.worker.poll=lambda:(_ for _ in ()).throw(OSError('test'));s.tick();self.assertIn('行情轮询继续',s.message)

    def test_manual_refresh_runs_once_even_with_automatic_updates_off(self):
        s=MacroStore();jobs=[]
        class Worker:
            def poll(self):return None
            def submit(self,job):jobs.append(job);return True
        s.worker=Worker();s.tick();self.assertFalse(jobs)
        s.refresh();s.tick();self.assertEqual(len(jobs),1);self.assertFalse(s.manual_pending)

    def test_late_result_from_old_binding_is_discarded_before_scheduling_new_codes(self):
        s=MacroStore();old=binding_hash(s.config);s.configure({'enabled':True,'codes':{**DEFAULT_CODES,'pmi':'M9999999'}})
        submitted=[]
        class Worker:
            def poll(self):return {'job':{'binding':old},'data':batch()}
            def submit(self,job):submitted.append(job);return True
        s.worker=Worker();s.tick()
        self.assertFalse(s.context()['macro']);self.assertEqual(s.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)
        self.assertEqual(submitted[0]['config']['codes']['pmi'],'M9999999')

    def test_same_automatic_config_and_immediate_refresh_use_local_http_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            board=WindDashboard(state_path=Path(directory)/'settings.json');server=make_server(board,0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            def post(path,payload,origin=None):
                headers={'Content-Type':'application/json'}
                if origin:headers['Origin']=origin
                return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers=headers),timeout=2)
            try:
                with post('/api/macro/config',{'enabled':True}) as r:self.assertEqual(json.load(r)['config']['codes'],DEFAULT_CODES)
                with post('/api/macro/refresh',{}) as r:self.assertEqual(r.status,202)
                with self.assertRaises(HTTPError) as raised:post('/api/macro/config',{'enabled':False},'https://example.invalid')
                self.assertEqual(raised.exception.code,403)
                with urlopen(base+'/api/snapshot') as r:self.assertIn('macro',json.load(r))
            finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_automatic_history_cli_reuses_stock_prices_and_never_relabels_old_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.json';output=root/'out'
            data={'schema':'barry-random-study-v1','plan':{'startDate':'2023-10-30','endDate':'2026-10-27'},
              'stocks':[{'code':'002466.SZ','dailyBars':[{'date':'2023-10-30'}],'hourlyBars':[{'unchanged':True}]}],'universe':{}}
            archive=root/'BarryMarketWatch/backtests/run-old';archive.mkdir(parents=True);save_json(archive/'回测数据.json',data)
            before=(archive/'回测数据.json').read_bytes()
            def collect(store,begin,end):
                self.assertEqual(begin,'2022-09-25');self.assertEqual(end,'2026-10-27')
                b=batch();store.append(b);return b
            with patch('random_backtest.sys.platform','win32'),patch.dict('os.environ',{'LOCALAPPDATA':str(root)}),patch.object(MacroStore,'collect_history',collect):
                main(['--wind-macro-latest','--output',str(output),'--no-browser'])
            after=json.loads((output/'回测数据.json').read_text())
            self.assertEqual(after['stocks'],data['stocks']);self.assertEqual(after['plan'],data['plan']);self.assertEqual(before,(archive/'回测数据.json').read_bytes())
            self.assertFalse(after['windMacroInfo']['historicalReleaseTimesVerified'])
            self.assertTrue(all(r['availableAt']>=NOW for r in after['context']['macro']))

if __name__=='__main__':unittest.main()
