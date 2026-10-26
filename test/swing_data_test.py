from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from swing_data import rows, fetch_history
from wind_data import CHINA,timestamp
from watchlist import WindDashboard


def result(times,values):
    return SimpleNamespace(ErrorCode=0,Fields=['OPEN','HIGH','LOW','CLOSE','VOLUME'],Times=times,Data=list(map(list,zip(*values))))


class SwingDataTest(unittest.TestCase):
    def test_daily_excludes_today_and_keeps_invalid_rows(self):
        values=[[100,102,99,101,100],[101,103,100,float('nan'),100],[102,104,101,103,100]]
        converted=rows(result([datetime(2026,10,d) for d in (22,23,26)],values),'daily',timestamp(datetime(2026,10,26,14)),CHINA,'2026-10-26')
        self.assertEqual(len(converted),2)
        self.assertFalse(converted[-1]['valid'])
        self.assertIsNone(converted[-1]['close'])

    def test_hourly_uses_ends_rejects_partial_and_future(self):
        times=[datetime(2026,10,26,10,30),datetime(2026,10,26,10,47),datetime(2026,10,26,11,30)]
        converted=rows(result(times,[[100,102,99,101,100]]*3),'hourly',timestamp(datetime(2026,10,26,10,47)),CHINA)
        self.assertEqual(len(converted),1)
        self.assertEqual(converted[0]['time'],timestamp(times[0]))

    def test_wind_fetch_uses_adjustment_and_calendar_and_resets_on_new_session(self):
        calls=[]
        class API:
            def tdays(self,*args):
                calls.append(('tdays',args));return SimpleNamespace(ErrorCode=0,Data=[[datetime(2026,10,d) for d in (22,23)]])
            def wsd(self,*args):
                calls.append(('wsd',args));return result([datetime(2026,10,23)],[[100,102,99,101,100]])
            def wsi(self,*args):
                calls.append(('wsi',args));return result([datetime(2026,10,26,10,30)],[[101,103,100,102,100]])
        data=fetch_history(API(),{'market':'cn','windCode':'600519.SH'},{'dailyBars':[{'date':'2026-10-22','close':999}]},CHINA,timestamp(datetime(2026,10,26,10,45)))
        self.assertEqual(data['expectedDailyDate'],'2026-10-23')
        self.assertEqual(data['dailyBars'][-1]['close'],101)
        self.assertEqual(calls[1][1][-1],'PriceAdj=F')
        self.assertEqual(calls[2][1][-1],'BarSize=60;PriceAdj=F')
        self.assertIn('2026-10-26',data['calendar'])
        with self.assertRaises(ValueError):
            class Broken(API):
                def tdays(self,*args):return SimpleNamespace(ErrorCode=-1,Data=[])
            fetch_history(Broken(),{'market':'cn','windCode':'600519.SH'},{},CHINA)

    def test_run_keeps_new_wind_calls_on_main_thread_and_stops_cleanly(self):
        board=WindDashboard(symbols=('SH.510300',))
        calls=[]
        def record(name):
            self.assertIs(threading.current_thread(),threading.main_thread())
            calls.append(name)
        class API:
            def start(self):record('start');return SimpleNamespace(ErrorCode=0)
            def stop(self):record('stop')
            def isconnected(self):record('connected');return True
            def wsq(self,codes,fields):
                record('wsq');now=datetime.now(CHINA)
                values=[int(now.strftime('%Y%m%d'))] if fields=='rt_date' else [100,99,10,int(now.strftime('%H%M%S'))]
                return SimpleNamespace(ErrorCode=0,Fields=fields.upper().split(','),Data=[[v] for v in values],Codes=[codes])
            def tdays(self,*args):record('tdays');return SimpleNamespace(ErrorCode=0,Data=[[datetime(2026,10,23)]])
            def wsd(self,*args):record('wsd');return result([datetime(2026,10,23)],[[100,102,99,101,100]])
            def wsi(self,*args):record('wsi');board.stop.set();return result([datetime(2026,10,26,10,30)],[[101,103,100,102,100]])
        with patch.dict(sys.modules,{'WindPy':SimpleNamespace(w=API())}):board.run()
        self.assertEqual(board.states['SH.510300']['swingIssue'],'')
        self.assertEqual(board.states['SH.510300']['swingRevision'],1)
        self.assertTrue(all(method in calls for method in ('start','tdays','wsd','wsi','stop')))

    def test_unknown_us_time_and_futures_do_not_fetch_or_enable_signals(self):
        board=WindDashboard()
        self.assertFalse(board.refresh_swing_history(object(),'US.AAPL'))
        self.assertIn('时间口径',board.states['US.AAPL']['swingIssue'])
        self.assertFalse(board.refresh_swing_history(object(),'SHFE.rb'))
        self.assertIn('期货',board.states['SHFE.rb']['swingIssue'])


if __name__=='__main__':unittest.main()
