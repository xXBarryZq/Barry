from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from watchlist import WindDashboard
from wind_dashboard import make_server
from security_catalog import SecurityCatalog, SettingsStore
from wind_data import CHINA


def result(fields,data,codes=None,error=0):
    return SimpleNamespace(Fields=fields,Data=data,Codes=codes or [],ErrorCode=error)


class WatchlistTest(unittest.TestCase):
    def test_code_resolution_name_search_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            board=WindDashboard(state_path=path)
            board.set_status(True,'test')
            self.assertTrue(board.search('600036.SH')['pending'])
            class API:
                def wss(self,code,fields,options=""):
                    self_test.assertEqual(fields,'sec_name')
                    return result(['SEC_NAME'],[['招商银行']],codes=[code])
            self_test=self
            board.resolve_commands(API())
            self.assertEqual(board.search('招商银行')['results'][0]['id'],'SH.600036')
            board.add_watch('600036.SH')
            board.add_watch('600036.SH')
            self.assertEqual(board.symbols.count('SH.600036'),1)
            restored=WindDashboard(state_path=path)
            self.assertIn('SH.600036',restored.symbols)
            restored.remove_watch('SH.600036')
            self.assertNotIn('SH.600036',WindDashboard(state_path=path).symbols)
            self.assertNotIn('quote',json.loads(path.read_text())['watchlist'][0])

    def test_wind_name_directory_loads_names_outside_seed_list(self):
        board=WindDashboard()
        class API:
            def wset(self,table,options):
                return result(['wind_code','sec_name'],[['600036.SH','601318.SH'],['招商银行','中国平安']])
        board.load_name_directory(API())
        self.assertEqual(board.search('中国平安')['results'][0]['windCode'],'601318.SH')
        self.assertIn('2',board.catalog.a_share_status)

    def test_failed_us_quotes_cannot_block_domestic_quotes(self):
        board=WindDashboard(symbols=('SH.510300','US.AAPL'))
        class API:
            def wsq(self,codes,fields):
                if 'AAPL.O' in codes:
                    return result([],[],error=-1)
                now=datetime.now(CHINA)
                return result(['RT_DATE'],[[int(now.strftime('%Y%m%d'))]],codes=[codes]) if fields=='rt_date' else result(['RT_LAST','RT_PRE_CLOSE','RT_VOL','RT_TIME'],[[4.2],[4.1],[100],[int(now.strftime('%H%M%S'))]],codes=[codes])
        board.refresh_quotes(API())
        self.assertEqual(board.states['SH.510300']['quote']['price'],4.2)
        self.assertIsNone(board.states['US.AAPL']['quote'])
        self.assertTrue(board.states['US.AAPL']['quoteIssue'])

    def test_unverified_us_timezone_preserves_price_but_never_creates_timestamp(self):
        board=WindDashboard(symbols=('US.AAPL',))
        class API:
            def wsq(self,codes,fields):
                return result(['RT_DATE'],[[20261022]],codes=[codes]) if fields=='rt_date' else result(['RT_LAST','RT_PRE_CLOSE','RT_VOL','RT_TIME'],[[200],[199],[100],[200100]],codes=[codes])
        board.refresh_quotes(API())
        quote=board.states['US.AAPL']['quote']
        self.assertEqual(quote['price'],200)
        self.assertIsNone(quote['timestamp'])
        self.assertIn('时区待确认',quote['timeLabel'])

    def test_directory_import_and_empty_watchlist_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            board=WindDashboard(symbols=(),state_path=path)
            self.assertEqual(board.import_directory('证券代码,证券简称\n513500.SH,标普500ETF\nMSFT.O,微软\n'),2)
            restored=WindDashboard(state_path=path)
            self.assertEqual(restored.symbols,[])
            self.assertEqual(restored.search('微软')['results'][0]['windCode'],'MSFT.O')
            self.assertEqual(restored.search('标普500ETF')['results'][0]['kind'],'ETF')

    def test_corrupt_settings_are_preserved_and_invalid_codes_cannot_reach_wind(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('invalid-json')
            with self.assertRaises(ValueError):
                WindDashboard(state_path=path)
            self.assertEqual(path.read_text(),'invalid-json')
        board=WindDashboard()
        board.set_status(True,'test')
        self.assertFalse(board.search('600036.SH;other=1')['pending'])


class EditableServerTest(unittest.TestCase):
    def test_http_add_remove_search_and_cross_origin_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            board=WindDashboard(state_path=Path(directory)/'state.json')
            board.import_directory('wind_code,sec_name\n601318.SH,中国平安\n')
            server=make_server(board,0)
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            base='http://127.0.0.1:'+str(server.server_port)
            def post(path,data,origin=None):
                request=Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**({'Origin':origin} if origin else {})})
                return urlopen(request,timeout=3)
            try:
                with urlopen(base+'/api/search?q=601318',timeout=3) as response:
                    self.assertEqual(json.load(response)['results'][0]['id'],'SH.601318')
                with self.assertRaises(HTTPError) as denied:
                    post('/api/watchlist',{'code':'SH.601318'},'https://attacker.invalid')
                self.assertEqual(denied.exception.code,403)
                with post('/api/watchlist',{'code':'SH.601318'}) as response:
                    self.assertEqual(response.status,200)
                self.assertIn('SH.601318',board.symbols)
                with post('/api/watchlist/remove',{'id':'SH.601318'}) as response:
                    self.assertEqual(response.status,200)
                self.assertNotIn('SH.601318',board.symbols)
            finally:
                board.stop.set();server.shutdown();server.server_close();worker.join(timeout=3)


if __name__=='__main__':
    unittest.main()
