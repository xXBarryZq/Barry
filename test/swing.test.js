import test from 'node:test';
import assert from 'node:assert/strict';
import {PROFILES,setups,evaluateTrades,currentSwing,prepareSwing,HOUR} from '../src/swing.js';
import {updatePosition} from '../src/positions.js';
import {AlertEngine} from '../public/alerts.js';
const p=PROFILES.short;
const b=(date,open=100,high=101,low=99,close=100,time=Date.parse(date+'T10:00:00Z'))=>({date,time,open,high,low,close,volume:100});
const buy={type:'buy'};

test('daily trend uses only earlier sessions; hourly signal needs breakout and volume',()=>{
  const daily=Array.from({length:90},(_,i)=>b(new Date(Date.UTC(2026,0,i+1)).toISOString().slice(0,10),100+i,101+i,99+i,100+i));
  const hours=Array.from({length:56},(_,i)=>({...b('2026-05-20',100,101,99,100,i*HOUR+1),volume:100}));
  hours.at(-1).high=104;hours.at(-1).close=103;hours.at(-1).volume=200;
  const baseline=setups(daily,hours,p);assert.equal(baseline.at(-1).type,'buy');
  assert.deepEqual(setups([...daily,b('2026-05-20',1,1,1,1),b('2026-05-21',1,1,1,1)],hours,p),baseline);
  assert.equal(setups(daily,hours.map((row,i)=>i===55?{...row,volume:100}:row),p).at(-1),null);
  assert.equal(setups(daily,hours.map((row,i)=>i===54?{...row,close:null}:row),p).at(-1),null);
});
test('entry is next open, target +5% succeeds without using signal close as entry',()=>{
  const hours=[b('2026-05-20',90,91,89,90),b('2026-05-21',100,106,99,105)];
  const r=evaluateTrades(hours,[buy,null],p,'us');assert.equal(r.completed,1);assert.equal(r.successRate,1);assert.equal(r.trades[0].entry,100);assert.equal(r.trades[0].exitPrice,105);
});
test('A-share T+1 blocks same-day target and loss breach is remembered until next session',()=>{
  const hours=[b('2026-05-20'),b('2026-05-20',100,106,85,100),b('2026-05-21',106,107,105,106)];
  const r=evaluateTrades(hours,[buy,null,null],p,'cn');assert.equal(r.completed,1);assert.equal(r.trades[0].exitDate,'2026-05-21');assert.equal(r.successRate,0);assert.equal(r.trades[0].riskBreached,true);
  assert.equal(evaluateTrades(hours.slice(0,2),[buy,null],p,'cn').pending,1);
});
test('same-bar target and risk ambiguity is excluded from successful trades',()=>{
  const r=evaluateTrades([b('2026-05-20'),b('2026-05-21',100,106,89,100)],[buy,null],p,'us');
  assert.equal(r.ambiguous,1);assert.equal(r.successRate,0);assert.equal(r.targetRate,0);
});
test('opening gaps execute at the opening loss rather than guaranteed stop',()=>{
  const hours=[b('2026-05-20'),b('2026-05-21'),b('2026-05-22',80,81,79,80)];
  const r=evaluateTrades(hours,[buy,null,null],p,'us');assert.equal(r.trades[0].exitPrice,80);assert.ok(Math.abs(r.trades[0].maxLoss-.2)<1e-10);assert.equal(r.successRate,0);
});
test('timeouts use end of final session and unfinished samples do not inflate rate',()=>{
  const hours=[b('2026-05-20'),b('2026-05-21'),b('2026-05-22'),b('2026-05-22',100,102,99,101),b('2026-05-25')];
  const r=evaluateTrades(hours,[buy,null,null,null,null],{...p,days:2},'cn');assert.equal(r.trades[0].exitTime,hours[3].time);assert.equal(r.trades[0].exitPrice,101);assert.equal(r.trades[0].exit,'timeout');
  const pending=evaluateTrades(hours.slice(0,2),[buy,null],p,'cn');assert.equal(pending.pending,1);assert.equal(pending.successRate,null);
});
test('missing history during an open trade prevents a claimed successful result',()=>{
  const hours=[b('2026-05-20'),b('2026-05-21'),{...b('2026-05-22'),low:null},b('2026-05-25',106,107,105,106)];
  const r=evaluateTrades(hours,[buy,null,null,null],p,'us');assert.equal(r.completed,1);assert.equal(r.successRate,0);assert.equal(r.ambiguous,1);
});
test('live signals suppress stale quotes, missing daily coverage, futures and retreated price',()=>{
  const now=Date.now(),last={...buy,label:'买入',resistance:101,support:99,time:now-1000};
  const prepared={p,last,lastBar:{...b('2026-05-21'),time:now-1000},dailyLast:{date:'2026-05-20'}};
  const item={market:'cn',expectedDailyDate:'2026-05-20',quote:{timestamp:now,price:103}};
  assert.equal(currentSwing(item,prepared,now).action.type,'buy');
  for(const changed of [{...item,quote:{timestamp:now-100000,price:103}},{...item,expectedDailyDate:'2026-05-21'},{...item,market:'futures'},{...item,quote:{timestamp:now,price:100}}])assert.equal(currentSwing(changed,prepared,now).action,null);
});
test('positions track both peak drawdown and loss from entry, respecting T+1',()=>{
  const position={entry:100,peak:120,date:'2026-05-20'};
  const r=updatePosition(position,{price:100,timestamp:1},p,'cn','2026-05-20');assert.equal(r.action.type,'risk');assert.equal(r.locked,true);assert.ok(r.drawdown>.16);assert.equal(r.loss,0);
  const next=updatePosition(position,{price:89,timestamp:2},p,'cn','2026-05-21');assert.equal(next.action.type,'sell');assert.match(next.action.reason,/相对买入价/);
});
test('alerts scan all events, baseline silently, deduplicate across reload and respect switch',()=>{
  const emitted=[],store=[];const engine=new AlertEngine({emit:e=>emitted.push(e),save:seen=>store.push(seen)});
  engine.scan([{key:'old'}],true);assert.equal(emitted.length,0);
  engine.scan([{key:'old'},{key:'unselected-short'},{key:'unselected-medium'}],true);assert.equal(emitted.length,2);
  engine.scan([{key:'unselected-short'}],true);assert.equal(emitted.length,2);
  engine.scan([{key:'disabled'}],false);engine.scan([{key:'enabled-again'}],true);assert.equal(emitted.length,2);
  const restored=new AlertEngine({seen:store.at(-1),emit:e=>emitted.push(e)});restored.scan([],true);restored.scan([{key:'unselected-short'}],true);assert.equal(emitted.length,2);
});

test('medium profile uses 20% target; a short target touch is insufficient',()=>{
  const hours=[b('2026-05-20'),b('2026-05-21',100,106,99,105),b('2026-05-22',121,122,120,121)];
  const r=evaluateTrades(hours,[buy,null,null],PROFILES.medium,'us');assert.equal(r.trades[0].exitPrice,121);assert.equal(r.successRate,1);
  assert.equal(evaluateTrades(hours.slice(0,2),[buy,null],PROFILES.medium,'us').pending,1);
});
test('trend sell is executed at next sellable open, not the signal close',()=>{
  const hours=[b('2026-05-20'),b('2026-05-21'),b('2026-05-22',99,100,98,99)];
  const r=evaluateTrades(hours,[buy,{type:'sell'},null],p,'us');assert.equal(r.trades[0].exit,'trend');assert.equal(r.trades[0].exitPrice,99);
});
test('manual positions include maximum holding period',()=>{
  const calendar=Array.from({length:21},(_,i)=>new Date(Date.UTC(2026,4,i+1)).toISOString().slice(0,10));
  const r=updatePosition({entry:100,date:'2026-05-01'}, {price:101,timestamp:1},p,'cn','2026-05-21',calendar);assert.match(r.action.reason,/持仓期限/);assert.equal(r.heldDays,21);
});
