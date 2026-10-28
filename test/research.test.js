import test from 'node:test';
import assert from 'node:assert/strict';
import {setups,evaluateTrades,PROFILES,HOUR} from '../src/swing.js';
import {researchSignals,runStudy,summarize} from '../tools/research/evaluate.js';

const day=i=>new Date(Date.UTC(2025,0,i+1)).toISOString().slice(0,10);
const bar=(date,price,time,volume=100)=>({date,time,open:price,high:price+.15,low:price-.15,close:price,volume});
function fixture(){
  const daily=Array.from({length:240},(_,i)=>({...bar(day(i),100+i*.25,Date.parse(day(i)+'T00:00:00Z')),low:100+i*.25-2.5}));
  const hours=Array.from({length:240},(_,i)=>{
    const date=day(180+Math.floor(i/4));
    return {...bar(date,145+i*.12,Date.parse(date+'T02:30:00Z')+(i%4)*HOUR,i%17===16?200:100),
      high:145+i*.12+.04,low:145+i*.12-.04,slot:['10:30','11:30','14:00','15:00'][i%4]};
  });
  return {schema:'barry-random-study-v1',plan:{startDate:day(180),endDate:day(239),splitDate:day(220),seed:20261028},
    universe:{note:'Synthetic software test only'},dataSource:'SYNTHETIC TEST, NOT MARKET PERFORMANCE',
    benchmark:{dailyBars:daily},stocks:[{code:'600001.SH',name:'软件测试，不是真实业绩',dailyBars:daily,hourlyBars:hours,calendar:daily.slice(180).map(b=>b.date),quality:{}}]};
}

test('research baseline uses the exact dashboard signal and execution functions',()=>{
  const data=fixture(),item=data.stocks[0],study=runStudy(data);
  for(const mode of ['short','medium']){
    const signals=setups(item.dailyBars,item.hourlyBars,PROFILES[mode]);
    assert.deepEqual(researchSignals(item,mode,'baseline',data.benchmark.dailyBars),signals);
    const boundary={date:'9999-12-31',time:item.hourlyBars.at(-1).time+HOUR};
    const expected=evaluateTrades([...item.hourlyBars,boundary],[...signals,null],PROFILES[mode],'cn',item.calendar);
    const actual=study.trades.filter(t=>t.mode===mode&&t.variant==='baseline').map(({code,name,mode,variant,segment,...t})=>t);
    assert.deepEqual(actual,expected.trades);
    assert.ok(expected.trades.length>0||mode==='medium');
  }
});
test('future stock and benchmark data cannot change earlier experimental signals',()=>{
  const data=fixture(),item=data.stocks[0],cut=day(210),altered=structuredClone(data);
  for(const row of altered.stocks[0].dailyBars)if(row.date>=cut)row.close*=.1;
  for(const row of altered.benchmark.dailyBars)if(row.date>=cut)row.close*=10;
  for(const row of altered.stocks[0].hourlyBars)if(row.date>=cut)row.volume*=100;
  for(const variant of ['market','pullback'])for(const mode of ['short','medium']){
    const before=researchSignals(item,mode,variant,data.benchmark.dailyBars).filter((_,i)=>item.hourlyBars[i].date<cut);
    const after=researchSignals(altered.stocks[0],mode,variant,altered.benchmark.dailyBars).filter((_,i)=>item.hourlyBars[i].date<cut);
    assert.deepEqual(after,before);
    if(mode==='short')assert.ok(before.some(s=>s?.type==='buy'),'causality test must include actual candidate signals');
  }
});
test('missing market history suppresses experimental entries and is visible in results',()=>{
  const data=fixture();data.benchmark={dailyBars:[],error:'test historical entitlement unavailable'};
  const study=runStudy(data);
  assert.match(study.benchmarkError,/entitlement/);
  assert.ok(study.trades.some(t=>t.variant==='baseline'));
  assert.equal(study.trades.filter(t=>t.variant!=='baseline').length,0);
});
test('profitable, target success, and uncertain outcomes have separate denominators',()=>{
  const t=(ret,success=false,uncertain=false)=>({return:ret,success,uncertain,maxDrawdown:.02,maxLoss:.01});
  const r=summarize([t(.02),t(.05,true),t(-.02),t(.5,false,true)]);
  assert.equal(r.completed,4);assert.equal(r.successRate,.25);assert.equal(r.positiveRate,2/3);
  assert.ok(Math.abs(r.meanReturn-.05/3)<1e-12);assert.equal(r.uncertain,1);
  assert.equal(summarize([]).meanReturn,null);
});
test('failed sampled stocks are retained and cannot inflate the loaded count',()=>{
  const data=fixture();data.stocks.push({code:'002466.SZ',name:'读取失败的测试样本',error:'test WSI unavailable'});
  const study=runStudy(data);
  assert.equal(study.stockCount,2);assert.equal(study.loaded,1);assert.equal(study.failures[0].code,'002466.SZ');
});
test('data-source diagnostics survive result export without treating failures as lost trades',()=>{
  const data=fixture();data.stocks=[{code:'002466.SZ',name:'数据缺失测试',error:'WSD all None',diagnostics:{adjustedDaily:{rows:987,valid:0}}}];
  const study=runStudy(data);
  assert.equal(study.loaded,0);assert.equal(study.trades.length,0);
  assert.equal(study.failures[0].diagnostics.adjustedDaily.rows,987);
  assert.ok(study.summaries.every(s=>s.successRate===null&&s.meanReturn===null));
});
test('cross-boundary holdings are separated from later-entry results',()=>{
  const data=fixture(),first=runStudy(data).trades[0];
  assert.ok(first);
  data.plan.splitDate=first.exitDate;
  const study=runStudy(data),found=study.trades.find(t=>t.entryTime===first.entryTime&&t.mode===first.mode&&t.variant===first.variant);
  assert.equal(found.segment,'cross-boundary');
  const later=study.summaries.find(s=>s.mode===first.mode&&s.variant===first.variant&&s.segment==='later');
  assert.equal(later.completed,study.trades.filter(t=>t.mode===first.mode&&t.variant===first.variant&&t.entryDate>=data.plan.splitDate).length);
});
test('the final completed session can finish an expired trade without a future price',()=>{
  const data=fixture(),item=data.stocks[0];
  const signals=setups(item.dailyBars,item.hourlyBars,PROFILES.medium),signalIndex=signals.findIndex(s=>s?.type==='buy');
  assert.ok(signalIndex>=0);
  const signal=item.hourlyBars[signalIndex],entryDate=day(241);
  const end=Array.from({length:60},(_,i)=>bar(day(241+i),200,Date.parse(day(241+i)+'T07:00:00Z')));
  const result=evaluateTrades([signal,...end,{date:'9999-12-31',time:end.at(-1).time+HOUR}],
    [{type:'buy'},...end.map(()=>null),null],PROFILES.medium,'cn',end.map(b=>b.date));
  assert.equal(result.trades[0].entryDate,entryDate);assert.equal(result.trades[0].exit,'timeout');
  assert.equal(result.trades[0].exitDate,end.at(-1).date);assert.equal(result.trades[0].heldDays,60);
});
