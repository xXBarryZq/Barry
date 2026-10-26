"""Regression cases for numeric autocomplete and name-service outages. SDK responses are fixtures."""
from datetime import datetime
import json
from pathlib import Path
import sys,tempfile,time,threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen,Request
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from security_catalog import candidates
from watchlist import WindDashboard
from wind_dashboard import make_server
from wind_data import CHINA


def result(fields=(),data=(),code='600036.SH',error=0):
    return SimpleNamespace(ErrorCode=error,Fields=list(fields),Data=list(data),Codes=[code])

class SearchTest(unittest.TestCase):
    def test_numeric_codes_and_copied_text_autocomplete_without_suffix(self):
        board=WindDashboard()
        for query,code in [('002466','002466.SZ'),('５１０３００','510300.SH'),('159915','159915.SZ'),('588000','588000.SH'),('920001','920001.BJ'),('002466天齐锂业','002466.SZ')]:
            with self.subTest(query=query):
                self.assertEqual(candidates(query),[code])
                rows=board.search(query)['results']
                self.assertEqual(rows[0]['windCode'],code)
        self.assertEqual(board.search('002466')['results'][0]['name'],'天齐锂业')
        self.assertFalse(board.search('00246')['pending'])
        self.assertEqual(board.search('00246')['results'][0]['symbol'],'002466')
        self.assertFalse(board.search('002466.SZ;foo=1')['results'])

    def test_unknown_names_are_labelled_and_can_be_added_then_renamed_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            board=WindDashboard(state_path=path);board.set_status(True,'test')
            row=board.search('600036')['results'][0]
            self.assertTrue(row['namePending']);self.assertIn('待核实',row['name'])
            board.add_watch(row['id'])
            class API:
                def wss(self,code,fields,options):return result(['SEC_NAME'],[['招商银行']],code)
            board.resolve_commands(API())
            restored=WindDashboard(state_path=path)
            self.assertEqual(restored.catalog.get('600036')['name'],'招商银行')
            self.assertFalse(restored.catalog.get('600036')['namePending'])
            self.assertIn('SH.600036',restored.symbols)

    def test_name_error_retains_quote_preview_and_adding_without_name_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            board=WindDashboard(state_path=path);board.set_status(True,'test')
            board.search('600036')
            class API:
                def wss(self,code,fields,options):return result(error=-40520007)
                def wsq(self,code,fields):return result(['RT_LAST'],[[10]],code)
            board.resolve_commands(API())
            data=board.search('600036.SH')
            self.assertTrue(data['canRetry']);self.assertIn('-40520007',data['message'])
            self.assertEqual(data['results'][0]['preview']['price'],10)
            self.assertTrue(data['results'][0]['namePending'])
            board.add_watch('600036')
            saved=path.read_text()
            self.assertNotIn('preview',saved);self.assertNotIn('readAt',saved)

    def test_failed_lookup_retries_after_cooldown_and_aliases_share_pending_job(self):
        board=WindDashboard();board.set_status(True,'test')
        self.assertTrue(board.search('600036')['pending'])
        self.assertTrue(board.search('600036.SH')['pending'])
        class API:
            count=0
            def wss(self,code,fields,options):
                self.count+=1
                return result(['SEC_NAME'],[['招商银行']],code) if self.count>1 else result(error=-1)
            def wsq(self,code,fields):return result(error=-1)
        api=API();board.resolve_commands(api);self.assertEqual(api.count,1)
        self.assertFalse(board.search('600036')['pending'])
        future=time.monotonic()+11
        with patch('watchlist.time.monotonic',return_value=future):
            self.assertTrue(board.search('600036')['pending']);board.resolve_commands(api)
        self.assertEqual(api.count,2)
        self.assertEqual(board.search('600036')['results'][0]['name'],'招商银行')

    def test_explicit_retry_and_exception_details_do_not_end_provider_loop(self):
        board=WindDashboard();board.set_status(True,'test');board.search('600036')
        class API:
            def wss(self,*args):raise TypeError('sensitive implementation string')
            def wsq(self,*args):raise RuntimeError('sensitive implementation string')
        board.resolve_commands(API())
        data=board.search('600036')
        self.assertIn('TypeError',data['message']);self.assertNotIn('sensitive',data['message'])
        board.last_attempt['600036.SH']-=3
        self.assertTrue(board.search('600036.SH',retry=True)['pending'])

    def test_invalid_added_symbol_cannot_block_existing_quotes(self):
        board=WindDashboard(symbols=('SH.510300',));board.add_watch(board.search('000000')['results'][0]['id'])
        class API:
            def wsq(self,codes,fields):
                if '000000' in codes:return result(error=-1)
                now=datetime.now(CHINA)
                return result(['RT_DATE'],[[int(now.strftime('%Y%m%d'))]],codes) if fields=='rt_date' else result(['RT_LAST','RT_PRE_CLOSE','RT_VOL','RT_TIME'],[[4.2],[4.1],[100],[int(now.strftime('%H%M%S'))]],codes)
        board.refresh_quotes(API())
        self.assertEqual(board.states['SH.510300']['quote']['price'],4.2)
        self.assertIsNone(board.states['SZ.000000']['quote'])
        self.assertTrue(board.states['SZ.000000']['quoteIssue'])

    def test_http_numeric_search_to_add_and_retry(self):
        board=WindDashboard();server=make_server(board,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/api/search?q=002466',timeout=3) as response:
                data=json.load(response);self.assertEqual(data['results'][0]['name'],'天齐锂业')
            with urlopen(Request(base+'/api/watchlist',data=b'{"code":"SZ.002466"}',headers={'Content-Type':'application/json'}),timeout=3) as response:self.assertEqual(response.status,200)
            with urlopen(base+'/api/search?q=002466&retry=1',timeout=3) as response:self.assertTrue(json.load(response)['results'][0]['added'])
            self.assertIn('SZ.002466',board.symbols)
        finally:
            board.stop.set();server.shutdown();server.server_close();worker.join(timeout=3)

if __name__=='__main__':unittest.main()
