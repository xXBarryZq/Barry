// One request at a time. Reconnection depends on successful data, never an opening socket.
export class MarketConnection {
  constructor({fetcher=globalThis.fetch.bind(globalThis),onSnapshot=()=>{},onConnection=()=>{},onError=()=>{},interval=3000,timeout=12000}={}) {
    Object.assign(this,{fetcher,onSnapshot,onConnection,onError,interval,timeout});
    this.active=false;this.pending=null;this.timer=null;this.controller=null;this.series=new Map();this.failures=0;this.lastServerTime=null;
  }
  start(){if(this.active)return;this.active=true;this.request();}
  async refresh(){if(this.pending)await this.pending;return this.request();}
  stop(){this.active=false;clearTimeout(this.timer);this.controller?.abort();}
  request(){
    if(this.pending)return this.pending;
    clearTimeout(this.timer);
    this.pending=this.read().finally(()=>{
      this.pending=null;
      if(this.active)this.timer=setTimeout(()=>this.request(),Math.min(this.interval*2**Math.min(this.failures,2),12000));
    });
    return this.pending;
  }
  async read(){
    const controller=new AbortController();this.controller=controller;
    const timeout=setTimeout(()=>controller.abort(),this.timeout);
    try{
      const versions=Object.fromEntries([...this.series].map(([id,row])=>[id,row.historyToken]));
      const query=new URLSearchParams({known:JSON.stringify(versions)});
      const response=await this.fetcher('/api/snapshot?'+query,{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw Error('看板读取失败（HTTP '+response.status+'）');
      const frame=await response.json();
      if(!Number.isFinite(frame.serverTime)||!Array.isArray(frame.instruments))throw Error('看板数据格式异常');
      if(frame.instanceId&&frame.instanceId!==this.serverId){this.lastServerTime=null;this.series.clear();}
      // Parallel button requests share this reader; older server frames cannot overwrite newer ones.
      if(this.lastServerTime!==null&&frame.serverTime<this.lastServerTime)return null;
      const next=new Map();
      frame.instruments=frame.instruments.map(row=>{
        const previous=this.series.get(row.id);
        if(row.historyOmitted){
          if(!previous||previous.historyToken!==row.historyToken)throw Error('历史缓存已失效，正在重新读取');
          row={...row,dailyBars:previous.dailyBars,hourlyBars:previous.hourlyBars,calendar:previous.calendar,bars:previous.bars};
        }
        next.set(row.id,{historyToken:row.historyToken,dailyBars:row.dailyBars||[],hourlyBars:row.hourlyBars||[],calendar:row.calendar||[],bars:row.bars||[]});
        return row;
      });
      // UI failures are reported separately; they must not masquerade as a socket failure.
      try{this.onSnapshot(frame);}catch(error){
        this.series.clear();this.failures++;this.onConnection(true);
        this.onError('行情数据处理失败（'+error.name+'），策略暂停并自动重试',{processing:true});return null;
      }
      this.series=next;this.serverId=frame.instanceId;this.lastServerTime=frame.serverTime;this.failures=0;
      this.onConnection(true);
      return frame;
    }catch(error){
      this.failures++;
      this.series.clear();
      if(this.active||error.name!=='AbortError'){
        this.onConnection(false);
        this.onError(error.name==='AbortError'?'本机看板响应超时，正在自动重试':error.message);
      }
      return null;
    }finally{clearTimeout(timeout);if(this.controller===controller)this.controller=null;}
  }
}
