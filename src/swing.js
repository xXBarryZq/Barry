// Long-only reference rules. Hour timestamps are interval ENDS; daily dates are exchange dates.
export const HOUR = 3_600_000;
export const PROFILES = {
  short: { id:'short', label:'短线', days:20, target:.05, drawdown:.10, loss:.10, fast:10, slow:30, breakout:20 },
  medium: { id:'medium', label:'中线', days:60, target:.20, drawdown:.20, loss:.20, fast:20, slow:60, breakout:55 }
};
export function profile(mode, settings={}) {
  const p={...PROFILES[mode]};
  if(!p.id)throw Error('未知策略周期');
  for(const key of ['drawdown','loss'])if(Number.isFinite(settings[key])&&settings[key]>0&&settings[key]<1)p[key]=settings[key];
  return p;
}
export const validBar = b => Boolean(b && Number.isFinite(b.time) && [b.open,b.high,b.low,b.close].every(v=>Number.isFinite(v)&&v>0) && b.low<=Math.min(b.open,b.close)&&b.high>=Math.max(b.open,b.close)&&b.low<=b.high&&Number.isFinite(b.volume)&&b.volume>=0);
const ema = (values,n) => values.slice(1).reduce((a,b)=>a+(b-a)*2/(n+1),values[0]);

export function setups(daily, hours, p) {
  const result=[];let di=0,tail=[];
  for(let i=0;i<hours.length;i++) {
    const bar=hours[i];
    // Today's daily candle is never visible to an intraday decision.
    while(di<daily.length&&daily[di].date<bar.date){const day=daily[di++];tail=validBar(day)?[...tail,day].slice(-180):[];}
    let setup=null;
    const prior=hours.slice(Math.max(0,i-p.breakout),i);
    if(validBar(bar)&&tail.length>=p.slow&&prior.length===p.breakout&&prior.every(validBar)) {
      const closes=tail.map(b=>b.close),fast=ema(closes,p.fast),slow=ema(closes,p.slow);
      const resistance=Math.max(...prior.map(b=>b.high)),support=Math.min(...prior.map(b=>b.low));
      const average=prior.reduce((sum,b)=>sum+b.volume,0)/prior.length;
      const volume=average>0&&bar.volume>=average*1.2;
      const type=volume&&fast>slow&&bar.close>resistance?'buy':volume&&fast<slow&&bar.close<support?'sell':null;
      if(type)setup={type,label:type==='buy'?'买入':'卖出',time:bar.time,barTime:bar.time,price:bar.close,resistance,support,mode:p.id,
        reason:`${p.label}：昨日及此前日线 EMA${p.fast}${type==='buy'?'高于':'低于'}EMA${p.slow}；60分钟收盘${type==='buy'?'突破':'跌破'}此前${p.breakout}根${type==='buy'?'高点':'低点'}；量≥均量1.2倍。`,
        note:type==='sell'?'供已有持仓减仓参考，不表示可做空。':'下一根可交易时段寻找成交机会；历史触发价不代表成交价。'};
    }
    result.push(setup);
  }
  return result;
}

// Evaluate non-overlapping long trades, entering at NEXT hourly open, never signal close.
// Conservative high-before-low ordering: unknown OHLC order cannot manufacture wins.
export function evaluateTrades(hours, signals, p, market='cn', calendar=[], entryGuard=null) {
  const sessions=[...new Set(calendar.length?calendar:hours.map(b=>b.date))].sort();
  const sessionIndex=new Map(sessions.map((day,i)=>[day,i]));
  const trades=[];let trade=null,pending=false,cancelledEntries=0;
  for(let i=0;i<hours.length;i++) {
    const b=hours[i];
    if(!validBar(b)) {
      if(trade){trade.uncertain=true;trade.dataGap=true;}
      pending=false;continue;
    }
    if(pending&&!trade) {
      // Research can cancel a pending entry using information known at this open.
      // Existing holdings always continue through the unchanged exit evaluator.
      if(!entryGuard||entryGuard(b.time-HOUR,b))
        trade={entry:b.open,entryDate:b.date,entryTime:b.time-HOUR,peak:b.open,maxDrawdown:0,maxLoss:0,riskBreached:false,targetReached:false,uncertain:false};
      else cancelledEntries++;
    }
    pending=false;
    if(trade) {
      const t=trade,held=(sessionIndex.get(b.date)??0)-(sessionIndex.get(t.entryDate)??0)+1;
      const sellable=market!=='cn'||b.date>t.entryDate;
      const target=t.entry*(1+p.target),stop=Math.max(t.peak*(1-p.drawdown),t.entry*(1-p.loss));
      let exit=null,exitPrice=null;
      // Gaps execute at opening price; never claim a guaranteed stop fill.
      if(sellable&&b.open<=stop){exit='risk';exitPrice=b.open;}
      else if(sellable&&b.open>=target){exit='target';exitPrice=b.open;t.targetReached=true;}
      else if(sellable&&t.exitPending){exit='trend';exitPrice=b.open;}
      if(exit) {
        t.maxDrawdown=Math.max(t.maxDrawdown,1-b.open/t.peak);t.maxLoss=Math.max(t.maxLoss,1-b.open/t.entry,0);
      } else {
        t.peak=Math.max(t.peak,b.high);
        t.maxDrawdown=Math.max(t.maxDrawdown,1-b.low/t.peak);t.maxLoss=Math.max(t.maxLoss,1-b.low/t.entry,0);
        const riskHit=b.low<=Math.max(t.peak*(1-p.drawdown),t.entry*(1-p.loss));
        const targetHit=b.high>=target;
        if(sellable&&riskHit&&targetHit){t.uncertain=true;exit='ambiguous';exitPrice=Math.min(b.close,Math.max(t.peak*(1-p.drawdown),t.entry*(1-p.loss)));}
        else if(sellable&&riskHit){exit='risk';exitPrice=Math.max(t.peak*(1-p.drawdown),t.entry*(1-p.loss));}
        else if(sellable&&targetHit){exit='target';exitPrice=target;t.targetReached=true;}
        // A T+1 locked day's target touch is not an achievable target sale.
        if(!exit&&sellable&&held>=p.days&&hours[i+1]?.date>b.date){exit='timeout';exitPrice=b.close;}
      }
      if(signals[i]?.type==='sell')t.exitPending=true;
      t.riskBreached ||= t.maxDrawdown>p.drawdown+1e-10||t.maxLoss>p.loss+1e-10;
      if(exit) {
        trades.push({...t,exit,exitDate:b.date,exitTime:b.time,exitPrice,heldDays:held,return:exitPrice/t.entry-1,
          success:exit==='target'&&!t.riskBreached&&!t.uncertain});
        trade=null;
      }
    }
    if(!trade&&signals[i]?.type==='buy'&&signals[i-1]?.type!=='buy')pending=true;
  }
  const completed=trades.length,successful=trades.filter(t=>t.success).length;
  return {trades,completed,successful,pending:trade?1:0,unfilled:pending?1:0,
    successRate:completed?successful/completed:null,
    targetRate:completed?trades.filter(t=>t.targetReached&&!t.uncertain).length/completed:null,
    riskRate:completed?trades.filter(t=>!t.riskBreached&&!t.uncertain).length/completed:null,
    ambiguous:trades.filter(t=>t.uncertain).length,openTrade:trade,cancelledEntries};
}

export function prepareSwing(item, mode, settings={}) {
  const p=profile(mode,settings),hours=item.hourlyBars||[],daily=item.dailyBars||[];
  const signals=setups(daily,hours,p);
  const evaluation=evaluateTrades(hours,signals,p,item.market,item.calendar||[]);
  return {p,signals,evaluation,last:signals.at(-1),lastBar:hours.at(-1),dailyLast:daily.at(-1)};
}
export function currentSwing(item, prepared, now=Date.now()) {
  const base={action:null,levels:null,barTime:prepared.lastBar?.time,reason:'当前未同时满足日线趋势、60分钟突破与放量条件。',period:'日线 + 60分钟'};
  if(item.market==='futures')return {...base,reason:'期货换月、夜盘交易日与杠杆口径待验证，波段提醒和成功率暂不启用。'};
  if(item.dataIssue||item.swingIssue)return {...base,reason:item.dataIssue||item.swingIssue};
  if(!item.quote||!Number.isFinite(item.quote.timestamp)||item.quote.timestamp>now+10_000||now-item.quote.timestamp>90_000)return {...base,reason:'行情过期或时间未确认，暂停提醒。'};
  if(!item.expectedDailyDate||prepared.dailyLast?.date!==item.expectedDailyDate)return {...base,reason:'日线未覆盖上一交易日，暂停提醒。'};
  if(!validBar(prepared.lastBar)||now-prepared.lastBar.time>90*60_000||prepared.lastBar.time>now)return {...base,reason:'等待新鲜、完整且已收盘的60分钟数据。'};
  const signal=prepared.last;
  const beyond=signal&&(signal.type==='buy'?item.quote.price>signal.resistance:item.quote.price<signal.support);
  if(!beyond)return base;
  return {...base,action:signal,reason:signal.reason,levels:{reference:signal.price,target:signal.price*(1+prepared.p.target),drawdown:signal.price*(1-prepared.p.drawdown),loss:signal.price*(1-prepared.p.loss)}};
}
