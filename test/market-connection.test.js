import test from 'node:test';
import assert from 'node:assert/strict';
import {MarketConnection} from '../public/market-connection.js';
const frame=(time=1,rows=[{id:'a',historyToken:'v1',dailyBars:[{close:10}],hourlyBars:[],calendar:[],bars:[]}])=>({serverTime:time,instruments:rows});
const response=value=>({ok:true,json:async()=>value});

test('polling keeps one in-flight request and successful frames reconnect without reloading',async()=>{
  let resolve;const seen=[],states=[];let requests=0;
  const c=new MarketConnection({fetcher:()=>{requests++;return new Promise(r=>resolve=r)},onSnapshot:f=>seen.push(f),onConnection:s=>states.push(s)});
  const a=c.request(),b=c.request();assert.equal(a,b);assert.equal(requests,1);
  resolve(response(frame()));await a;assert.deepEqual(states,[true]);assert.equal(seen.length,1);
});
test('partial snapshots retain exact histories while applying changed prices',async()=>{
  const frames=[frame(),frame(2,[{id:'a',historyToken:'v1',historyOmitted:true,quote:{price:11}}])],queries=[],seen=[];
  const c=new MarketConnection({fetcher:async url=>{queries.push(url);return response(frames.shift())},onSnapshot:f=>seen.push(f)});
  await c.request();await c.request();assert.equal(seen[1].instruments[0].dailyBars[0].close,10);assert.equal(seen[1].instruments[0].quote.price,11);
  assert.deepEqual(JSON.parse(new URL('http://local'+queries[1]).searchParams.get('known')),{a:'v1'});
});
test('transport failure pauses, clears versions, then recovers on a later frame',async()=>{
  let calls=0;const states=[],errors=[],queries=[];
  const c=new MarketConnection({fetcher:async url=>{queries.push(url);if(++calls===2)throw Error('offline');return response(frame(calls));},onConnection:s=>states.push(s),onError:e=>errors.push(e)});
  await c.request();await c.request();await c.request();assert.deepEqual(states,[true,false,true]);assert.deepEqual(errors,['offline']);assert.deepEqual(JSON.parse(new URL('http://local'+queries[2]).searchParams.get('known')),{});
});
test('a timed-out body aborts and the next request can recover',async()=>{
  let count=0;const errors=[];
  const c=new MarketConnection({timeout:20,fetcher:async(url,{signal})=>++count===1?{ok:true,json:()=>new Promise((r,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))))}:response(frame()),onError:e=>errors.push(e)});
  c.active=true;await c.request();c.stop();assert.match(errors[0],/超时/);assert.ok(await c.request());c.stop();
});
test('processing failures are reported without inventing a disconnected socket',async()=>{
  const states=[],errors=[];let fail=true;
  const c=new MarketConnection({fetcher:async()=>response(frame()),onSnapshot:()=>{if(fail)throw TypeError('bad field')},onConnection:s=>states.push(s),onError:(message,detail)=>errors.push({message,detail})});
  await c.request();assert.deepEqual(states,[true]);assert.equal(errors[0].detail.processing,true);
  fail=false;assert.ok(await c.request());
});
test('restart identity invalidates old versions even if computer clock moves backwards',async()=>{
  const frames=[{...frame(100),instanceId:'first'},{...frame(50),instanceId:'second'}],seen=[];
  const c=new MarketConnection({fetcher:async()=>response(frames.shift()),onSnapshot:f=>seen.push(f)});
  await c.request();await c.request();assert.equal(seen.length,2);assert.equal(seen[1].serverTime,50);
});
test('stop aborts its own request and never schedules a new background poll',async()=>{
  let count=0;
  const c=new MarketConnection({interval:5,fetcher:(url,{signal})=>{count++;return new Promise((r,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))))}});
  c.start();const pending=c.pending;c.stop();await pending;assert.equal(c.timer,null);assert.equal(count,1);
});
test('a successful webpage read never rewrites old source quote time',async()=>{
  const q={price:10,timestamp:1,receivedAt:2};let result;
  const c=new MarketConnection({fetcher:async()=>response(frame(Date.now(),[{id:'a',quote:q}])),onSnapshot:f=>result=f});
  await c.request();assert.equal(result.instruments[0].quote.timestamp,1);assert.equal(result.instruments[0].quote.receivedAt,2);
});
