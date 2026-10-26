"""Optional local reference data; never modifies trading rules or sends credentials."""
import csv
from datetime import date, datetime
import importlib
import io
import threading
import time

from security_catalog import SettingsStore, candidates

SOURCES={'Bloomberg','Capital IQ'}
FIELDS={'PE_RATIO':'市盈率','PX_TO_BOOK_RATIO':'市净率','EQY_DVD_YLD_IND':'预期股息率','CRNCY':'币种'}


def bbg_security(wind_code):
    codes=candidates(wind_code)
    if len(codes)!=1:
        raise ValueError('请使用完整Wind代码')
    symbol,exchange=codes[0].rsplit('.',1)
    if exchange in ('SH','SZ','BJ'):
        return symbol+' CH Equity'
    if exchange in ('O','N','P','A'):
        return symbol+' US Equity'
    raise ValueError('Bloomberg补充资料目前只支持股票和ETF')


def read_bloomberg(wind_code):
    try:
        blpapi=importlib.import_module('blpapi')
    except ImportError as error:
        raise ValueError('当前Python未安装Bloomberg官方blpapi。可先导入Excel导出的CSV；账号/Excel可用不等于Desktop API已开通。') from error
    options=blpapi.SessionOptions()
    options.setServerHost('localhost');options.setServerPort(8194)
    options.setConnectTimeout(2000);options.setNumStartAttempts(1)
    session=blpapi.Session(options)
    rows=[]
    try:
        if not session.start():
            raise ValueError('未连接到本机Bloomberg Desktop API，请确认同机终端登录及接口权限。')
        if not session.openService('//blp/refdata'):
            raise ValueError('Bloomberg参考资料服务不可用。')
        request=session.getService('//blp/refdata').createRequest('ReferenceDataRequest')
        request.getElement('securities').appendValue(bbg_security(wind_code))
        for field in FIELDS:request.getElement('fields').appendValue(field)
        session.sendRequest(request)
        deadline=time.monotonic()+12
        complete=False
        while time.monotonic()<deadline:
            event=session.nextEvent(500)
            for message in event:
                if message.hasElement('responseError'):
                    raise ValueError('Bloomberg返回权限或请求错误，请核对终端授权。')
                if not message.hasElement('securityData'):
                    continue
                securities=message.getElement('securityData')
                for index in range(securities.numValues()):
                    security=securities.getValueAsElement(index)
                    if security.hasElement('securityError'):
                        continue
                    fields=security.getElement('fieldData')
                    for field in FIELDS:
                        if fields.hasElement(field) and not fields.getElement(field).isNull():
                            value=fields.getElement(field).getValue()
                            rows.append({'wind_code':wind_code,'source':'Bloomberg','field':field,'value':str(value),
                                         'unit':'%' if field=='EQY_DVD_YLD_IND' else '倍' if field in ('PE_RATIO','PX_TO_BOOK_RATIO') else '',
                                         'as_of':'','method':'Desktop API','read_at':datetime.now().astimezone().isoformat()})
            if event.eventType()==blpapi.Event.RESPONSE:
                complete=True;break
        if not complete:
            raise ValueError('Bloomberg查询超时，未保存不完整结果。')
        if not rows:
            raise ValueError('Bloomberg未返回可用字段，可能无权限、映射不符或该品种不适用。')
        return rows
    finally:
        session.stop()


class ResearchStore:
    def __init__(self,path=None):
        self.store=SettingsStore(path)
        self.lock=threading.RLock()
        self.records=self.store.data.get('records',[])
        if not isinstance(self.records,list) or any(not isinstance(row,dict) or not all(key in row for key in ('wind_code','source','field')) for row in self.records):
            raise ValueError('补充资料文件格式无效')
        self.busy=False
        self.message='未直连。可导入Capital IQ/Bloomberg导出的资料；同机Bloomberg可按需查询。'

    def snapshot(self,wind_code):
        with self.lock:
            return {'records':[dict(row) for row in self.records if row.get('wind_code')==wind_code],
                    'busy':self.busy,'message':self.message}

    def put(self,rows):
        with self.lock:
            merged={(row['wind_code'],row['source'],row['field']):row for row in self.records}
            for row in rows:merged[(row['wind_code'],row['source'],row['field'])]=row
            if len(merged)>5000:
                raise ValueError('补充资料最多5000项，请缩小导入范围。')
            values=list(merged.values());self.store.save({'records':values});self.records=values
        return len(rows)

    def import_csv(self,text,default_source=None):
        rows=[]
        for raw in csv.DictReader(io.StringIO(text.lstrip('\ufeff'))):
            row={str(k).strip().lower():str(v or '').strip() for k,v in raw.items() if k is not None}
            source=row.get('source') or default_source
            codes=candidates(row.get('wind_code') or row.get('证券代码',''))
            # Wide Excel export: one security per row, selected source supplied by UI.
            if not row.get('field') and len(codes)==1 and source in SOURCES:
                as_of=row.get('as_of') or row.get('数据日期','')
                if as_of:date.fromisoformat(as_of)
                aliases={'pe_ratio':'PE_RATIO','市盈率':'PE_RATIO','px_to_book_ratio':'PX_TO_BOOK_RATIO','市净率':'PX_TO_BOOK_RATIO','eqy_dvd_yld_ind':'EQY_DVD_YLD_IND','股息率':'EQY_DVD_YLD_IND','crncy':'CRNCY','币种':'CRNCY'}
                found=False
                for key,field in aliases.items():
                    value=row.get(key,'')
                    if not value:continue
                    if len(value)>500:raise ValueError('单项补充资料过长')
                    rows.append({'wind_code':codes[0],'source':source,'field':field,'value':value,'unit':'%' if field=='EQY_DVD_YLD_IND' else '倍' if field in ('PE_RATIO','PX_TO_BOOK_RATIO') else '', 'as_of':as_of,'method':'CSV导入','read_at':datetime.now().astimezone().isoformat()})
                    found=True
                if not found:raise ValueError('宽表需含PE_RATIO/市盈率、PX_TO_BOOK_RATIO/市净率等已支持列；其他字段可使用field,value长表。')
                if len(rows)>5000:raise ValueError('一次最多导入5000项')
                continue
            if source not in SOURCES or len(codes)!=1 or not row.get('field') or not row.get('value'):
                raise ValueError('每行需要完整wind_code、source（Bloomberg或Capital IQ）、field、value。')
            as_of=row.get('as_of','')
            if as_of:
                date.fromisoformat(as_of)
            if any(len(row.get(key,''))>500 for key in ('field','value','unit','as_of')):
                raise ValueError('单项补充资料过长')
            rows.append({'wind_code':codes[0],'source':source,'field':row['field'],'value':row['value'],
                         'unit':row.get('unit',''),'as_of':as_of,'method':'CSV导入','read_at':datetime.now().astimezone().isoformat()})
            if len(rows)>5000:
                raise ValueError('一次最多导入5000项')
        if not rows:
            raise ValueError('CSV为空或列名无效')
        return self.put(rows)

    def query(self,wind_code):
        bbg_security(wind_code)
        with self.lock:
            if self.busy:
                raise ValueError('Bloomberg已有查询在进行，请稍候。')
            self.busy=True;self.message='正在查询本机Bloomberg补充资料；Wind行情继续运行。'
        def work():
            try:
                self.put(read_bloomberg(wind_code))
                message='Bloomberg查询完成。数据时点未提供时不以读取时间替代；字段缺失可能受权限限制。'
            except ValueError as error:
                message=str(error)
            except Exception:
                message='Bloomberg接口不可用，请核对官方SDK、Python版本、同机登录和Desktop API权限。'
            finally:
                with self.lock:
                    self.busy=False;self.message=message
        threading.Thread(target=work,daemon=True,name='barry-bloomberg-reference').start()
