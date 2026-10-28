// Offline research only. The running dashboard continues to use src/swing.js.
import {PROFILES,validBar,setups,evaluateTrades,HOUR} from '../../src/swing.js';

export const VARIANTS = [
  {id:'baseline',label:'现有价量规则'},
  {id:'market',label:'价量规则＋市场趋势过滤'},
  {id:'pullback',label:'回调入场实验（仅短线价量部分）'}
];
const researchEma=(values,n)=>values.slice(1).reduce((a,b)=>a+(b-a)*2/(n+1),values[0]);
const mean=values=>values.reduce((a,b)=>a+b,0)/values.length;
const median=values=>{const a=[...values].sort((x,y)=>x-y),i=Math.floor(a.length/2);return a.length%2?a[i]:(a[i-1]+a[i])/2;};

function marketGate(daily,date){
  const prior=daily.filter(b=>b.date<date).slice(-65);
  if(prior.length<65||!prior.every(validBar))return false;
  const closes=prior.map(b=>b.close);
  return closes.at(-1)>mean(closes.slice(-60))&&mean(closes.slice(-20))>mean(closes.slice(-25,-5));
}
function dailyContext(daily,date){
  const prior=daily.filter(b=>b.date<date).slice(-180);
  if(prior.length<65||!prior.every(validBar))return null;
  const closes=prior.map(b=>b.close),fast=researchEma(closes,20),slow=researchEma(closes,60);
  const tr=prior.slice(-14).map((b,i)=>{const prev=prior[prior.length-15+i];return Math.max(b.high-b.low,Math.abs(b.high-prev.close),Math.abs(b.low-prev.close));});
  const atr=mean(tr); // Explicit simple mean TR14, not a claim of Wilder smoothing.
  let touch=false;
  for(let i=prior.length-5;i<prior.length;i++){
    const c=closes.slice(0,i+1),b=prior[i];
    if(b.close<researchEma(c,60))return null;
    if(Math.abs(b.low-researchEma(c,20))<=atr)touch=true;
  }
  return closes.at(-1)>fast&&fast>slow&&touch&&atr>0;
}

export function researchSignals(item,mode,variant,benchmark){
  const p=PROFILES[mode],hours=item.hourlyBars,daily=item.dailyBars;
  const baseline=setups(daily,hours,p);
  if(variant==='baseline')return baseline;
  const gate=new Map(),contexts=new Map(),slots=new Map();
  return hours.map((bar,i)=>{
    const clock=bar.slot||new Date(bar.time+8*HOUR).toISOString().slice(11,16);
    const previousSlot=slots.get(clock)||[];
    if(validBar(bar))slots.set(clock,[...previousSlot,bar].slice(-20));
    if(baseline[i]?.type==='sell')return baseline[i];
    if(!gate.has(bar.date))gate.set(bar.date,marketGate(benchmark,bar.date));
    if(!gate.get(bar.date)||!validBar(bar))return null;
    if(variant==='market')return baseline[i]?.type==='buy'?baseline[i]:null;
    if(mode!=='short')return null;
    if(!contexts.has(bar.date))contexts.set(bar.date,dailyContext(daily,bar.date));
    const context=contexts.get(bar.date),prior=hours.slice(Math.max(0,i-3),i);
    if(!context||prior.length!==3||!prior.every(validBar)||previousSlot.length<20)return null;
    const resistance=Math.max(...prior.map(b=>b.high)),volume=median(previousSlot.map(b=>b.volume));
    return bar.close>resistance&&volume>0&&bar.volume>=volume*1.2?
      {type:'buy',time:bar.time,resistance}:null;
  });
}

export function summarize(trades){
  const certain=trades.filter(t=>!t.uncertain),wins=certain.filter(t=>t.return>0),losses=certain.filter(t=>t.return<0);
  return {completed:trades.length,certain:certain.length,uncertain:trades.length-certain.length,
    successes:trades.filter(t=>t.success).length,
    successRate:trades.length?trades.filter(t=>t.success).length/trades.length:null,
    positiveRate:certain.length?wins.length/certain.length:null,
    meanReturn:certain.length?mean(certain.map(t=>t.return)):null,
    meanWin:wins.length?mean(wins.map(t=>t.return)):null,
    meanLoss:losses.length?mean(losses.map(t=>t.return)):null,
    worstReturn:certain.length?Math.min(...certain.map(t=>t.return)):null,
    maxDrawdown:trades.length?Math.max(...trades.map(t=>t.maxDrawdown)):null,
    maxLoss:trades.length?Math.max(...trades.map(t=>t.maxLoss)):null};
}

export function runStudy(dataset){
  if(dataset.schema!=='barry-random-study-v1'||!Array.isArray(dataset.stocks))throw Error('回测资料格式不符');
  const results=[],trades=[],split=dataset.plan.splitDate;
  for(const item of dataset.stocks){
    if(item.error)continue;
    const hours=item.hourlyBars;
    if(!hours?.length)continue;
    const priced=hours.filter(validBar),first=priced[0],last=priced.at(-1);
    const buyHoldReturn=first?.date===dataset.plan.startDate&&last?.date===dataset.plan.endDate?last.close/first.open-1:null;
    // A boundary marker closes an expired trade on the final *completed* session.
    // It contains no price and cannot produce a new transaction.
    const boundary={date:'9999-12-31',time:hours.at(-1).time+HOUR,open:null,high:null,low:null,close:null,volume:null};
    const evaluationHours=[...hours,boundary];
    for(const mode of ['short','medium'])for(const variant of VARIANTS){
      if(mode==='medium'&&variant.id==='pullback')continue;
      const p=PROFILES[mode];
      const signals=researchSignals(item,mode,variant.id,dataset.benchmark?.dailyBars||[]);
      const initial=signals.map((signal,i)=>hours[i].date>=dataset.plan.startDate?signal:null);
      const result=evaluateTrades(evaluationHours,[...initial,null],p,'cn',item.calendar);
      const all=result.trades.map(t=>({...t,code:item.code,name:item.name,mode,variant:variant.id,
        segment:t.entryDate<split?(t.exitDate<split?'earlier':'cross-boundary'):'later'}));
      trades.push(...all);
      results.push({code:item.code,name:item.name,mode,variant:variant.id,...summarize(all),
        open:result.pending||0,unfilled:result.unfilled||0,
        dataQuality:item.quality,buyHoldReturn,openTrade:result.openTrade||null});
    }
  }
  const summaries=[];
  for(const mode of ['short','medium'])for(const variant of VARIANTS){
    if(mode==='medium'&&variant.id==='pullback')continue;
    const own=trades.filter(t=>t.mode===mode&&t.variant===variant.id);
    const rows=results.filter(t=>t.mode===mode&&t.variant===variant.id);
    for(const segment of ['all','earlier','later','cross-boundary']){
      const selected=segment==='all'?own:own.filter(t=>t.segment===segment);
      summaries.push({mode,variant:variant.id,segment,...summarize(selected),
        open:segment==='all'?rows.reduce((n,r)=>n+r.open,0):null});
    }
  }
  return {schema:'barry-random-results-v1',plan:dataset.plan,universe:dataset.universe,
    scope:'仅价量与市场趋势；未验证估值、行业、盈利改善、大宗交易等完整增强理论。',
    dataSource:dataset.dataSource,rulesHash:dataset.evaluationEngineSha256||dataset.plan.engineSha256||null,
    benchmarkError:dataset.benchmark?.error||null,stockCount:dataset.stocks.length,
    loaded:dataset.stocks.filter(s=>!s.error).length,
    failures:dataset.stocks.filter(s=>s.error).map(s=>({code:s.code,name:s.name,error:s.error})),
    quality:dataset.stocks.filter(s=>!s.error).map(s=>({code:s.code,...s.quality})),
    results,summaries,trades};
}

const pct=x=>Number.isFinite(x)?(100*x).toFixed(2)+'%':'—';
export function renderStudy(dataset,root=document.getElementById('study')){
  const study=runStudy(dataset),doc=root.ownerDocument;
  const el=(tag,text,parent=root)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;parent.append(node);return node;};
  root.replaceChildren();
  el('h1','随机A股历史回测 · 初步检查');
  el('p',`固定样本 ${study.stockCount} 只；成功读取 ${study.loaded} 只。${study.plan.startDate} 至 ${study.plan.endDate}，随机种子 ${study.plan.seed}。`);
  el('p',study.scope);
  if(dataset.plan.engineSha256&&dataset.evaluationEngineSha256&&dataset.plan.engineSha256!==dataset.evaluationEngineSha256)
    el('p','注意：本次计算规则与采集数据时的规则指纹不同，应作为新版本结果保存，不能替代原先结果。');
  el('p','这里的“成功”是期限内可卖出时达到短线5%／中线20%，且此前两项风险均未超限。“赚钱比例”和“平均盈亏”另列。收益均基于下述撮合假设估算。平均盈亏仅统计无数据或成交歧义的已完成交易，不能当作账户收益率；若有失败或不确定样本，更不能据此证明盈利。');
  if(study.benchmarkError)el('p','市场过滤数据不可用：'+study.benchmarkError+'。两个实验版本没有买入信号不代表市场条件不满足。');
  el('p','本工具没有自动判定策略有效。一次固定抽样、近三年、未包含全部财务与交易限制，只能做初步检查。结果可能推翻某项假设，不能证明未来仍然有效。');
  const button=(label,filename,content,type)=>{const b=el('button',label);b.onclick=()=>{const url=URL.createObjectURL(new Blob([content],{type}));const a=doc.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};};
  button('保存结果JSON（含逐笔交易）','回测结果.json',JSON.stringify(study,null,2),'application/json;charset=utf-8');
  const fields=['code','name','mode','variant','segment','entryDate','exitDate','entry','exitPrice','return','exit','success','maxDrawdown','maxLoss','uncertain'];
  const csv=v=>{const s=String(v??''),safe=/^[\s\u0000-\u001f]*[=+@-]/.test(s)?"'"+s:s;return '"'+safe.replaceAll('"','""')+'"';};
  button('保存逐笔交易CSV','逐笔交易.csv','\ufeff'+[fields.map(csv).join(','),...study.trades.map(t=>fields.map(f=>csv(t[f])).join(','))].join('\r\n'),'text/csv;charset=utf-8');
  const table=(headers,rows)=>{const t=el('table'),head=el('tr',undefined,el('thead',undefined,t)),body=el('tbody',undefined,t);for(const h of headers)el('th',h,head);for(const row of rows){const tr=el('tr',undefined,body);for(const v of row)el('td',String(v??'—'),tr);}};
  const label=id=>VARIANTS.find(v=>v.id===id)?.label||id;
  const segmentLabels={all:'全期',earlier:'前段',later:'后段', 'cross-boundary':'跨分段交易'};
  el('h2','不同规则的结果');
  table(['周期／规则','区间','完成／不确定','严格达标率','赚钱比例¹','平均盈亏¹','最差单笔¹','未结束'],study.summaries.map(r=>[
    `${r.mode==='short'?'短线':'中线'} · ${label(r.variant)}`,segmentLabels[r.segment],`${r.completed} / ${r.uncertain}`,
    pct(r.successRate),pct(r.positiveRate),pct(r.meanReturn),pct(r.worstReturn),r.open]));
  el('p',`¹ 只统计无歧义的已完成样本，不确定样本可能存在选择偏差。分段日期 ${study.plan.splitDate}；跨界交易单列，不把前段买入、后段卖出算作独立后段结果。未结束持仓不进入达标率分母，也不计入已完成平均收益。`);
  el('h2','抽样及数据核查');
  el('p',study.universe.note);
  table(['代码','名称','状态','缺失／异常日线','缺失／异常小时','一字价小时','首／末有效小时日期'],dataset.stocks.map(s=>[
    s.code,s.name,s.error||'已读取',s.quality?.invalidDaily,s.quality?.invalidHourly,s.quality?.flatHourly,
    s.quality?`${s.quality.firstHourDate||'—'} / ${s.quality.lastHourDate||'—'}`:'—']));
  el('h2','逐股结果');
  table(['代码／名称','周期／规则','完成／不确定','严格达标率','平均盈亏¹','同期持有涨幅²'],study.results.map(r=>[
    `${r.code} ${r.name}`,`${r.mode==='short'?'短线':'中线'} · ${label(r.variant)}`,`${r.completed} / ${r.uncertain}`,pct(r.successRate),pct(r.meanReturn),pct(r.buyHoldReturn)]));
  el('p','² 同期持有是研究首日首个有效小时开盘至末日最后有效小时收盘的前复权价格涨幅，仅作行情背景；持有时间、资金占用与单笔策略不同，不能用它与平均单笔收益直接判断谁更好。缺少期初或期末价格时不计算。');
  el('h2','解释边界');
  for(const text of [
    'T+1、跳空越过风险线、缺失数据、同根目标与止损顺序不明均保守处理。全小时最高价等于最低价时不假定可以买卖。缺少逐笔订单、历史涨跌停及停牌状态，其他成交能力仍未被完整验证。',
    '价格统一到同一日线前复权基准；这是价格收益近似，未逐项重建分红到账、税费和账户现金流。手续费按约定忽略。',
    '没有信号不是成功。数据读取失败的股票保留在抽样名单中，不换成另一只；失败部分的结果未知。',
    '三个版本使用同一现有交易评估器，入场均为信号后的下一小时开盘，退出规则保持一致。回调实验仅替换入场、增加市场过滤，ATR采用14日真实波幅的简单平均；没有实现完整增强方案的财务筛选、结构止损、最低盈亏比和日线EMA退出。因此不能将其结果称为完整研究方案的胜率。',
    '先检查后段表现、样本数量、数据失败及不确定交易，再讨论是否继续验证。即使某项平均盈亏为正，也需扩大样本、保留独立区间并前向模拟。'
  ])el('p',text);
  el('p',`读取时间：${dataset.collectedAt}。生成报告不提交订单、不读取账户和持仓，不改变看板交易逻辑。`);
  return study;
}
