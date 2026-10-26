"""Own only our child process: slow Wind history/native calls cannot block quote polling."""
import importlib
import multiprocessing
import queue
import time

from swing_data import fetch_history
from wind_data import get_zone


def history_loop(requests, results, api_module='WindPy'):
    api=None
    try:
        while True:
            job=requests.get()
            if job is None:
                break
            try:
                first=api is None
                if first:
                    api=importlib.import_module(api_module).w
                if first or not api.isconnected():
                    started=api.start()
                    if getattr(started,'ErrorCode',None)!=0 or not api.isconnected():
                        raise ValueError('历史数据进程未连接Wind；报价轮询继续，稍后自动重试。')
                if job['kind']=='directory':
                    response=api.wset('sectorconstituent','sectorid=a001010100000000')
                    fields=[str(value).upper() for value in (getattr(response,'Fields',None) or [])]
                    if getattr(response,'ErrorCode',None)!=0 or not {'WIND_CODE','SEC_NAME'}.issubset(fields):
                        raise ValueError('名称目录未加载，仍可按六位代码添加。')
                    codes,names=response.Data[fields.index('WIND_CODE')],response.Data[fields.index('SEC_NAME')]
                    data={'directory':[{'wind_code':code,'sec_name':name} for code,name in zip(codes,names)]}
                else:
                    zone=get_zone(job['zone'])
                    if zone is None:
                        raise ValueError('历史数据时区未确认')
                    data=fetch_history(api,job['item'],job['state'],zone)
                results.put({'id':job['id'],'data':data,'finishedAt':time.monotonic()})
            except Exception as error:
                message=str(error) if isinstance(error,ValueError) else '历史数据接口异常（'+type(error).__name__+'）；报价轮询继续。'
                results.put({'id':job['id'],'error':message,'finishedAt':time.monotonic()})
    finally:
        if api is not None:
            try:api.stop()
            except Exception:pass


class HistoryWorker:
    def __init__(self,timeout=45,api_module='WindPy',target=history_loop):
        self.context=multiprocessing.get_context('spawn')
        self.timeout,self.api_module,self.target=timeout,api_module,target
        self.process=None;self.requests=None;self.results=None;self.job=None;self.serial=0

    def submit(self,job):
        if self.job is not None:
            return False
        if self.process is None or not self.process.is_alive():
            self.close()
            self.requests=self.context.Queue(maxsize=1);self.results=self.context.Queue(maxsize=1)
            self.process=self.context.Process(target=self.target,args=(self.requests,self.results,self.api_module),daemon=True,name='barry-wind-history')
            self.process.start()
        self.serial+=1
        self.job={**job,'id':self.serial,'deadline':time.monotonic()+self.timeout}
        self.requests.put(self.job)
        return True

    def poll(self):
        if self.job is None:
            return None
        try:
            result=self.results.get_nowait()
        except queue.Empty:
            result=None
        if result is not None and result.get('id')==self.job['id'] and result.get('finishedAt',float('inf'))<=self.job['deadline']:
            job,self.job=self.job,None
            return {'job':job,**result}
        if time.monotonic()>self.job['deadline'] or self.process is None or not self.process.is_alive():
            job=self.job
            self.close()
            return {'job':job,'error':'历史查询超时或进程已退出；报价轮询继续，稍后自动重试。'}
        return None

    def close(self):
        process=self.process
        if process is not None:
            if process.is_alive():
                try:self.requests.put_nowait(None)
                except (queue.Full,OSError,ValueError):pass
                process.join(timeout=.2)
            if process.is_alive():
                process.terminate();process.join(timeout=.5)
            if not process.is_alive():process.close()
        for channel in (self.requests,self.results):
            if channel is not None:
                channel.cancel_join_thread();channel.close()
        self.process=None;self.requests=None;self.results=None;self.job=None
