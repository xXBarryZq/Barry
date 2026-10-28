import test from 'node:test';
import assert from 'node:assert/strict';
import {createContextPolicy} from '../tools/research/context.js';
import {evaluateTrades,PROFILES,HOUR} from '../src/swing.js';
const t=s=>Date.parse(s+'+08:00'),now=t('2026-10-28T10:30:00');
function data(){return {schema:'barry-context-v1',issues:[],macro:[
  {indicator:'cn_pmi',observationDate:'2026-08-31',availableAt:t('2026-08-31T09:30:00'),value:50.2},
  {indicator:'cn_pmi',observationDate:'2026-09-30',availableAt:t('2026-09-30T09:30:00'),value:50.4},
  {indicator:'cn_funding',observationDate:'2026-10-26',availableAt:t('2026-10-26T18:00:00'),value:1.6,referenceValue:1.5},
  {indicator:'cn_funding',observationDate:'2026-10-27',availableAt:t('2026-10-27T18:00:00'),value:1.7,referenceValue:1.5}],
  events:[],coverage:['CN','002466.SZ'].map(scope=>({scope,startAt:t('2026-10-01T00:00:00'),endAt:t('2026-11-01T00:00:00')}))};}
const risk=()=>({id:'test-risk',scope:'002466.SZ',type:'company_risk',action:'pause_new_entries',
  availableAt:t('2026-10-28T09:00:00'),assessedAt:t('2026-10-28T09:10:00'),startAt:t('2026-10-28T09:00:00'),
  endAt:t('2026-10-29T09:00:00'),source:'SYNTHETIC',reference:'not actual market news'});
test('macro units, consecutive funding pressure and independent PMI deterioration',()=>{
  const d=data();assert.equal(createContextPolicy(d).macroAt(now).allow,true);
  for(const r of d.macro.filter(r=>r.indicator==='cn_funding'))r.value=1.8;
  let s=createContextPolicy(d).macroAt(now);assert.equal(s.allow,false);assert.match(s.reason,/资金面/);
  d.macro.at(-2).value=1.6;assert.equal(createContextPolicy(d).macroAt(now).allow,true);
  d.macro[1].value=49.5;s=createContextPolicy(d).macroAt(now);assert.equal(s.allow,false);assert.match(s.reason,/PMI/);
});
test('macro requires published releases, freshness, and consecutive PMI months',()=>{
  for(const change of [d=>d.macro.pop(),d=>d.macro[1].availableAt=now+1,d=>d.macro[0].observationDate='2026-07-31',
    d=>d.macro.at(-1).referenceValue=null]){
    const d=data();change(d);assert.equal(createContextPolicy(d).macroAt(now).known,false);
  }
  assert.equal(createContextPolicy(data()).macroAt(t('2026-12-15T10:30:00')).known,false);
});
test('late publication of older data cannot replace newer observations',()=>{
  const d=data();d.macro.push({...d.macro[0],observationDate:'2026-07-31',availableAt:now-1,value:40});
  assert.deepEqual(createContextPolicy(d).macroAt(now).pmi,[50.4,50.2]);
});
test('empty news needs market AND security coverage; information alone never vetoes',()=>{
  const d=data();assert.equal(createContextPolicy(d).eventsAt('002466.SZ',now).allow,true);
  assert.equal(createContextPolicy(d).eventsAt('600519.SH',now).known,false);
  d.coverage=d.coverage.filter(r=>r.scope!=='CN');assert.equal(createContextPolicy(d).eventsAt('002466.SZ',now).known,false);
  d.coverage=data().coverage;d.events=[{...risk(),action:'information'}];
  assert.equal(createContextPolicy(d).eventsAt('002466.SZ',now).allow,true);
});
test('assessments cannot be backdated and event windows expire',()=>{
  const d=data();d.events=[risk()];const p=createContextPolicy(d);
  assert.equal(p.eventsAt('002466.SZ',now).allow,false);
  assert.equal(p.eventsAt('002466.SZ',t('2026-10-28T09:05:00')).allow,true);
  assert.equal(p.eventsAt('002466.SZ',risk().endAt).allow,true);
  d.events[0].assessedAt=t('2026-10-30T00:00:00');
  assert.equal(createContextPolicy(d).eventsAt('002466.SZ',now).allow,true);
});
test('unknown and adverse conditions differ and validation errors isolate modules',()=>{
  const d=data();d.events=[risk()];let p=createContextPolicy(d);
  assert.equal(p.at('002466.SZ',now,'context').known,true);assert.equal(p.at('002466.SZ',now,'context').allow,false);
  d.issues=[{module:'events',message:'invalid timestamp'}];p=createContextPolicy(d);
  assert.equal(p.at('002466.SZ',now,'events').known,false);assert.equal(p.at('002466.SZ',now,'macro').known,true);
  assert.equal(createContextPolicy(null).at('002466.SZ',now,'context').allow,false);
});
test('future macro and news cannot change earlier decisions',()=>{
  const d=data(),before=createContextPolicy(d).at('002466.SZ',now,'context');
  d.events=[{...risk(),availableAt:now+1,assessedAt:now+2}];
  d.macro.push({indicator:'cn_pmi',observationDate:'2026-10-31',availableAt:now+86400000*5,value:40});
  assert.deepEqual(createContextPolicy(d).at('002466.SZ',now,'context'),before);
});
test('observed revisions take effect only when seen and never count as two periods',()=>{
  const d=data();d.macroSourcePolicy='locally_observed';d.macro.push({...d.macro[1],value:49,availableAt:now+1000});
  const p=createContextPolicy(d);assert.equal(p.macroAt(now).allow,true);assert.equal(p.macroAt(now+1000).allow,false);
  assert.match(p.policy.id,/observed/);assert.deepEqual(p.macroAt(now+1000).pmi,[49,50.2]);
});
test('next-open recheck can cancel entries but never disables existing risk exits',()=>{
  const b=(date,price,time)=>({date,time,open:price,high:price+.1,low:price-.1,close:price,volume:100});
  const bars=[b('2026-10-27',100,t('2026-10-27T15:00:00')),b('2026-10-28',100,t('2026-10-28T10:30:00')),
    b('2026-10-29',80,t('2026-10-29T10:30:00'))];
  const signal=[{type:'buy'},null,null],seen=[];
  const r=evaluateTrades(bars,signal,PROFILES.short,'cn',[],at=>{seen.push(at);return false;});
  assert.equal(r.completed,0);assert.equal(r.pending,0);assert.equal(r.cancelledEntries,1);assert.deepEqual(seen,[bars[1].time-HOUR]);
  const held=evaluateTrades(bars,signal,PROFILES.short,'cn',[],at=>at===bars[1].time-HOUR);
  assert.equal(held.trades[0].exit,'risk');assert.equal(held.trades[0].exitPrice,80);
});
