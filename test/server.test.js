import test from 'node:test';
import assert from 'node:assert/strict';
import { createMarketServer } from '../src/server.js';

async function running(options, run) {
  const app = createMarketServer(options);
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  try { await run(`http://127.0.0.1:${app.server.address().port}`, app.feed); } finally { await app.close(); }
}

test('demo serves the application, labelled simulated quotes, and SSE updates', async () => {
  await running({ mode: 'demo' }, async base => {
    const home = await fetch(base);
    assert.equal(home.status, 200);
    assert.match(await home.text(), /市场观察台/);
    assert.ok(home.headers.get('content-security-policy').includes("script-src 'self'"));
    const snapshot = await (await fetch(`${base}/api/snapshot`)).json();
    assert.equal(snapshot.mode, 'demo');
    assert.equal(snapshot.instruments.length, 12);
    assert.ok(snapshot.instruments.every(item => item.quote?.price > 0 && item.bars.length >= 21));
    const controller = new AbortController();
    const stream = await fetch(`${base}/api/events`, { signal: controller.signal });
    assert.equal(stream.headers.get('content-type'), 'text/event-stream; charset=utf-8');
    const reader = stream.body.getReader();
    const event = new TextDecoder().decode((await reader.read()).value);
    assert.match(event, /"mode":"demo"/);
    assert.match(event, /^retry: 3000/);
    controller.abort();
    await reader.cancel().catch(() => {});
    const post = await fetch(`${base}/api/ingest`, { method: 'POST' });
    assert.equal(post.status, 409);
  });
});

test('live ingestion requires authentication, rejects invalid batches atomically, and exposes new quotes', async () => {
  await running({ mode: 'live', token: 'test-only-token-not-a-credential' }, async base => {
    const data = { id: 'US.SPY', timestamp: Date.now(), price: 601.5, previousClose: 600, volume: 1200 };
    const post = (quotes, authorized = true) => fetch(`${base}/api/ingest`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(authorized ? { Authorization: 'Bearer test-only-token-not-a-credential' } : {}) }, body: JSON.stringify({ quotes }) });
    assert.equal((await post([data], false)).status, 401);
    assert.equal((await post([data, { ...data, id: 'UNKNOWN' }])).status, 400);
    let snapshot = await (await fetch(`${base}/api/snapshot`)).json();
    assert.ok(snapshot.instruments.every(item => !item.quote));
    assert.equal((await post([data])).status, 200);
    snapshot = await (await fetch(`${base}/api/snapshot`)).json();
    const spy = snapshot.instruments.find(item => item.id === 'US.SPY');
    assert.equal(spy.quote.price, 601.5);
    assert.equal(spy.strategy.direction, 'waiting');
    assert.equal((await post([{ ...data, timestamp: data.timestamp - 1 }])).status, 400);
    assert.equal((await fetch(`${base}/.env`)).status, 404);
    assert.equal((await fetch(`${base}/src/server.js`)).status, 404);
  });
});

test('live mode without a token does not accept quotes or fabricate data', async () => {
  await running({ mode: 'live' }, async base => {
    assert.equal((await fetch(`${base}/api/ingest`, { method: 'POST' })).status, 503);
    const health = await (await fetch(`${base}/api/health`)).json();
    assert.equal(health.ingestionConfigured, false);
    assert.equal(health.freshQuotes, 0);
  });
});

test('demo search adds a security outside the initial list and can remove it', async()=>{
  await running({mode:'demo'},async base=>{
    const search=await (await fetch(`${base}/api/search?q=${encodeURIComponent('中国平安')}`)).json();
    assert.equal(search.results[0].id,'SH.601318');
    assert.equal(search.results[0].added,false);
    const post=(path,body,origin)=>fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json',...(origin?{Origin:origin}:{})},body:JSON.stringify(body)});
    assert.equal((await post('/api/watchlist',{code:'SH.601318'},'http://attacker.invalid')).status,403);
    assert.equal((await post('/api/watchlist',{code:'SH.601318'})).status,200);
    let snapshot=await (await fetch(base+'/api/snapshot')).json();
    assert.ok(snapshot.instruments.find(item=>item.id==='SH.601318').quote.price>0);
    await post('/api/watchlist/remove',{id:'SH.601318'});
    snapshot=await (await fetch(base+'/api/snapshot')).json();
    assert.ok(!snapshot.instruments.some(item=>item.id==='SH.601318'));
  });
});

test('demo numeric stock and ETF autocomplete preserves leading zeros and fullwidth digits',async()=>{
  await running({mode:'demo'},async base=>{
    for(const [query,name,id] of [['002466','天齐锂业','SZ.002466'],['５１０３００','沪深300 ETF','SH.510300'],['159915','创业板 ETF','SZ.159915']]){
      const data=await (await fetch(base+'/api/search?q='+encodeURIComponent(query))).json();
      assert.equal(data.results[0].id,id);assert.equal(data.results[0].name,name);
    }
  });
});
