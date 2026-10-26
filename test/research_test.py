from pathlib import Path
import sys,tempfile,time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/windows'))
from research import ResearchStore, bbg_security, read_bloomberg

class ResearchTest(unittest.TestCase):
    def test_long_and_wide_exports_preserve_source_dates_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'research.json'
            store=ResearchStore(path)
            self.assertEqual(store.import_csv('wind_code,source,field,value,unit,as_of\n600519.SH,Capital IQ,PE_RATIO,18,倍,2026-09-30\n'),1)
            self.assertEqual(store.import_csv('证券代码,数据日期,市盈率,市净率\n600519.SH,2026-10-20,19,5\n','Bloomberg'),2)
            data=ResearchStore(path).snapshot('600519.SH')['records']
            self.assertEqual(len(data),3)
            self.assertEqual({row['source'] for row in data},{'Capital IQ','Bloomberg'})
            self.assertEqual(data[0]['as_of'],'2026-09-30')
            self.assertEqual(data[0]['method'],'CSV导入')
            self.assertEqual(ResearchStore(path).snapshot('SPY.P')['records'],[])

    def test_bad_exports_are_atomic_and_unknown_dates_stay_unknown(self):
        store=ResearchStore()
        with self.assertRaises(ValueError):store.import_csv('wind_code,source,field,value,as_of\n600519.SH,Capital IQ,PE_RATIO,18,2026-09-30\n600519.SH,Bloomberg,PE_RATIO,19,bad-date\n')
        self.assertEqual(store.records,[])
        store.import_csv('wind_code,source,field,value\nAAPL.O,Bloomberg,PE_RATIO,30\n')
        self.assertEqual(store.records[0]['as_of'],'')
        with self.assertRaises(ValueError):store.import_csv('wind_code,PE_RATIO\n600519.SH;foo=1,1\n','Capital IQ')

    def test_bloomberg_mapping_and_absent_sdk_does_not_start_or_ask_for_password(self):
        self.assertEqual(bbg_security('600519.SH'),'600519 CH Equity')
        self.assertEqual(bbg_security('AAPL.O'),'AAPL US Equity')
        with self.assertRaises(ValueError):bbg_security('RB.SHF')
        with patch('research.importlib.import_module',side_effect=ImportError):
            with self.assertRaisesRegex(ValueError,'blpapi'):read_bloomberg('AAPL.O')

    def test_optional_query_is_nonblocking_and_failure_is_reported(self):
        store=ResearchStore()
        with patch('research.read_bloomberg',side_effect=ValueError('接口未开通')):
            store.query('AAPL.O')
            for _ in range(100):
                if not store.snapshot('AAPL.O')['busy']:break
                time.sleep(.01)
        self.assertEqual(store.snapshot('AAPL.O')['message'],'接口未开通')
        self.assertEqual(store.records,[])

if __name__=='__main__':unittest.main()
