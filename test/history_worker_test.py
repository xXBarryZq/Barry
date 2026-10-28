from datetime import datetime
from pathlib import Path
import json,os,sys,tempfile,time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from history_worker import HistoryWorker
from watchlist import WindDashboard
from wind_dashboard import make_server
from wind_data import CHINA
from urllib.request import urlopen
import threading

SDK='''
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import os,threading,time
release=Path(RELEASE)
entered=Path(ENTERED)
class API:
    connected=False
    def check(self):assert threading.current_thread() is threading.main_thread()
    def start(self):self.check();self.connected=True;return SimpleNamespace(ErrorCode=0)
    def stop(self):self.check();self.connected=False
    def isconnected(self):self.check();return self.connected
    def tdays(self,*args):self.check();return SimpleNamespace(ErrorCode=0,Data=[[datetime(2026,10,23)]])
    def wset(self,*args):self.check();return SimpleNamespace(ErrorCode=0,Fields=['wind_code','sec_name'],Data=[['002466.SZ'],['天齐锂业']])
    def wsd(self,*args):self.check();return self.result([datetime(2026,10,23)])
    def wsi(self,*args):
        self.check();entered.write_text(str(os.getpid()))
        limit=time.monotonic()+10
        while not release.exists() and time.monotonic()<limit:time.sleep(.01)
        return self.result([datetime(2026,10,26,10,30)])
    def result(self,times):return SimpleNamespace(ErrorCode=0,Fields=['OPEN','HIGH','LOW','CLOSE','VOLUME'],Times=times,Data=[[100],[102],[99],[101],[100]])
w=API()
'''


def wait_until(predicate,timeout=6):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.02)
    raise AssertionError('condition timed out')


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        self.release=self.root/'release';self.entered=self.root/'entered'
        (self.root/'fake_history_sdk.py').write_text('RELEASE='+repr(str(self.release))+'\nENTERED='+repr(str(self.entered))+'\n'+SDK)
        sys.path.insert(0,str(self.root))
        self.worker=HistoryWorker(timeout=6,api_module='fake_history_sdk')
    def tearDown(self):
        self.worker.close();sys.path.remove(str(self.root));self.directory.cleanup()
    def job(self):
        return {'kind':'history','symbol':'SH.510300','token':'test','zone':'Asia/Shanghai','item':{'windCode':'510300.SH','market':'cn'},'state':{}}

    def test_blocked_native_history_is_separate_while_http_and_quotes_keep_updating(self):
        board=WindDashboard(symbols=('SH.510300',));board.set_status(True,'test')
        class Quotes:
            n=0
            def wsq(self,codes,fields):
                self.n+=1;now=datetime.now(CHINA)
                values=[int(now.strftime('%Y%m%d'))] if fields=='rt_date' else [100+self.n,99,10,int(now.strftime('%H%M%S'))]
                return SimpleNamespace(ErrorCode=0,Fields=fields.upper().split(','),Codes=[codes],Data=[[v] for v in values])
        self.assertTrue(self.worker.submit(self.job()));wait_until(self.entered.exists)
        self.assertNotEqual(int(self.entered.read_text()),os.getpid())
        server=make_server(board,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            api=Quotes();prices=[]
            for _ in range(3):
                board.refresh_quotes(api)
                with urlopen('http://127.0.0.1:'+str(server.server_port)+'/api/snapshot',timeout=2) as response:
                    frame=json.load(response);prices.append(frame['instruments'][0]['quote']['price'])
            self.assertEqual(len(set(prices)),3);self.assertIsNone(self.worker.poll())
            self.release.touch();result=wait_until(self.worker.poll)
            self.assertIn('data',result);self.assertEqual(result['data']['dailyBars'][-1]['close'],101)
        finally:board.stop.set();server.shutdown();server.server_close();thread.join(timeout=2)

    def test_timeout_terminates_only_our_child_and_next_job_restarts(self):
        self.worker.timeout=.5;self.worker.submit(self.job())
        result=wait_until(self.worker.poll)
        self.assertIn('超时',result['error']);self.assertIsNone(self.worker.process)
        self.worker.timeout=6;self.worker.submit({'kind':'directory'})
        result=wait_until(self.worker.poll);self.assertEqual(result['data']['directory'][0]['sec_name'],'天齐锂业')

    def test_macro_native_queries_run_in_child_main_thread_without_blocking_quotes(self):
        sdk=self.root/'fake_history_sdk.py'
        extra='''    def edb(self,code,begin,end,options):
        self.check();entered.write_text(str(os.getpid()))
        while not release.exists():time.sleep(.01)
        return SimpleNamespace(ErrorCode=0,Codes=[code],Times=[datetime(2026,10,26)],Data=[[50 if code=='M0017126' else 1.5]])
'''
        sdk.write_text(sdk.read_text().replace('    def result(self,times):',extra+'    def result(self,times):'))
        from wind_macro import config_value
        self.worker.submit({'kind':'macro','config':config_value({}),'begin':'2026-10-01','end':'2026-10-28'})
        wait_until(self.entered.exists);self.assertNotEqual(int(self.entered.read_text()),os.getpid())
        board=WindDashboard(symbols=('SH.510300',));board.set_status(True,'quotes continue')
        self.assertTrue(board.snapshot()['status']['connected']);self.assertIsNone(self.worker.poll())
        self.release.touch();result=wait_until(self.worker.poll)
        self.assertEqual(len(result['data']['series']),3);self.assertTrue(all(s['points'] for s in result['data']['series']))

    def test_stale_history_response_after_timezone_reset_is_discarded(self):
        board=WindDashboard(symbols=('US.AAPL',));old=board.states['US.AAPL']['historyToken'];board.set_zones({'usBarZone':'UTC'})
        class Worker:
            job={'pending':True}
            def poll(self):return {'job':{'kind':'history','symbol':'US.AAPL','token':old},'data':{'dailyBars':[{'close':999}]}}
        board.service_history(Worker());self.assertEqual(board.states['US.AAPL']['dailyBars'],[])

    def test_same_history_payload_does_not_recalculate_browser_versions(self):
        board=WindDashboard(symbols=('SH.510300',));state=board.states['SH.510300'];token=state['historyToken']
        data={key:state[key] for key in ('dailyBars','hourlyBars','calendar','expectedDailyDate')}
        class Worker:
            job={'pending':True}
            def poll(self):return {'job':{'kind':'history','symbol':'SH.510300','token':token},'data':data}
        board.service_history(Worker());self.assertEqual(state['swingRevision'],0);self.assertEqual(state['historyToken'],token)

    def test_real_wind_disconnect_retries_start_and_resumes_quote_polling(self):
        board=WindDashboard(symbols=('SH.510300',));board.poll_seconds=0;starts=[]
        class API:
            def isconnected(self):return len(starts)>=2
            def start(self):starts.append(1);return SimpleNamespace(ErrorCode=0 if len(starts)>=2 else -1)
            def stop(self):pass
            def wsq(self,codes,fields):
                now=datetime.now(CHINA)
                values=[int(now.strftime('%Y%m%d'))] if fields=='rt_date' else [100,99,10,int(now.strftime('%H%M%S'))]
                if board.quote_polls>=2:board.stop.set()
                return SimpleNamespace(ErrorCode=0,Fields=fields.upper().split(','),Codes=[codes],Data=[[v] for v in values])
        class Worker:
            job={'pending':True}
            def poll(self):return None
            def close(self):pass
        with patch.dict(sys.modules,{'WindPy':SimpleNamespace(w=API())}),patch.object(board.stop,'wait',side_effect=lambda interval:board.stop.is_set()):board.run(history_worker=Worker())
        self.assertEqual(len(starts),2);self.assertGreaterEqual(board.quote_polls,2);self.assertTrue(board.connected)


if __name__=='__main__':unittest.main()
