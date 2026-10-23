import test from 'node:test';
import assert from 'node:assert/strict';
import { analyze, actionHistory, MINUTE, STALE_AFTER } from '../src/strategy.js';
import { instruments } from '../src/instruments.js';
import { Feed, validateUpdate } from '../src/feed.js';

const now = Math.floor(Date.now() / MINUTE) * MINUTE + 30_000;
const instrument = instruments[0];
const quote = { price: 104, previousClose: 100, volume: 5000, timestamp: now };
const bars = Array.from({ length: 40 }, (_, index) => ({ time: Math.floor(now / MINUTE) * MINUTE - (40 - index) * MINUTE, open: 100 + index * 0.1, high: 100.15 + index * 0.1, low: 99.95 + index * 0.1, close: 100.1 + index * 0.1, volume: 100 }));

test('uptrend produces a volatility-based invalidation below reference, observation above', () => {
  const result = analyze(instrument, quote, bars, now);
  assert.equal(result.direction, 'bullish');
  assert.ok(result.levels.invalidation < result.levels.reference);
  assert.ok(result.levels.observation > result.levels.reference);
  assert.ok(result.atr > 0);
});

test('declining equity prices describe risk without suggesting short permission', () => {
  const descending = bars.map(bar => ({ ...bar, open: 210 - bar.open, high: 210 - bar.low, low: 210 - bar.high, close: 210 - bar.close }));
  const result = analyze(instrument, quote, descending, now);
  assert.equal(result.direction, 'bearish');
  assert.ok(result.levels.invalidation > result.levels.reference);
  assert.match(result.levels.basis, /不代表可做空/);
});

test('a forming candle spike does not change the closed-candle strategy', () => {
  const baseline = analyze(instrument, quote, bars, now);
  const forming = { time: Math.floor(now / MINUTE) * MINUTE, open: 100, high: 500, low: 99, close: 500, volume: 99999 };
  assert.deepEqual(analyze(instrument, quote, [...bars, forming], now), baseline);
});

test('stale quotes or stale candles suppress strategy levels', () => {
  const staleQuote = analyze(instrument, { ...quote, timestamp: now - STALE_AFTER - 1 }, bars, now);
  assert.equal(staleQuote.direction, 'stale');
  assert.equal(staleQuote.levels, null);
  const staleBars = analyze(instrument, quote, bars.map(bar => ({ ...bar, time: bar.time - 5 * MINUTE })), now);
  assert.equal(staleBars.direction, 'stale');
  assert.equal(staleBars.levels, null);
});

test('unknown quote dates and future timestamps cannot enable a strategy', () => {
  for (const timestamp of [null, undefined, now + MINUTE]) {
    const result = analyze(instrument, { ...quote, timestamp }, bars, now);
    assert.equal(result.direction, 'waiting');
    assert.equal(result.levels, null);
    assert.match(result.reason, /日期或时间尚未核实/);
  }
});

test('insufficient history does not emit a strategy', () => {
  assert.equal(analyze(instrument, quote, bars.slice(-20), now).direction, 'waiting');
  assert.equal(analyze(instrument, null, bars, now).direction, 'waiting');
});

test('missing latest prices never produce a trend or price levels', () => {
  const missing = bars.map((bar, index) => index === bars.length - 1 ? { ...bar, close: NaN } : bar);
  const result = analyze(instrument, quote, missing, now);
  assert.equal(result.direction, 'waiting');
  assert.equal(result.levels, null);
  assert.match(result.reason, /缺失或无效/);
});

test('a missing minute cannot be bypassed by dropping its row', () => {
  const missing = bars.map((bar, index) => index === bars.length - 2 ? { ...bar, close: NaN } : bar);
  for (const history of [missing, missing.filter(bar => Number.isFinite(bar.close))]) {
    const result = analyze(instrument, quote, history, now);
    assert.equal(result.direction, 'waiting');
    assert.equal(result.levels, null);
    assert.match(result.reason, /当前连续 1 根/);
  }
});

test('a strategy recovers only after enough valid contiguous minute bars', () => {
  const history = bars.map((bar, index) => index === 10 ? { ...bar, close: NaN } : bar);
  const result = analyze(instrument, quote, history, now);
  assert.equal(result.direction, 'bullish');
  assert.ok(result.levels);
  const tail = bars.slice(11);
  assert.deepEqual(result, analyze(instrument, quote, tail, now));
});

test('a trend alone never emits a buy label; breakout and volume must both confirm', () => {
  assert.equal(analyze(instrument,quote,bars,now).action,null);
  const breakout=bars.map((bar,index)=>index===bars.length-1?{...bar,volume:200}:bar);
  assert.equal(analyze(instrument,quote,breakout,now).action.type,'buy');
  assert.equal(analyze(instrument,{...quote,price:breakout.at(-2).close},breakout,now).action,null);
  const noBreakout=breakout.map((bar,index)=>index===breakout.length-1?{...bar,close:bar.open}:bar);
  assert.equal(analyze(instrument,quote,noBreakout,now).action,null);
  const badVolume=breakout.map((bar,index)=>index===breakout.length-1?{...bar,volume:NaN}:bar);
  assert.equal(analyze(instrument,quote,badVolume,now).action,null);
});

test('sell labels require a downside break and volume and describe existing positions', () => {
  const descending=bars.map((bar,index)=>({...bar,open:210-bar.open,high:210-bar.low,low:210-bar.high,close:210-bar.close,volume:index===bars.length-1?200:100}));
  const action=analyze(instrument,quote,descending,now).action;
  assert.equal(action.type,'sell');
  assert.match(action.note,/已有持仓/);
  assert.equal(analyze(instrument,{...quote,price:descending.at(-2).close},descending,now).action,null);
  assert.equal(analyze(instrument,{...quote,timestamp:now-STALE_AFTER-1},descending,now).action,null);
});

test('historical markers cannot see unclosed future candles or bridge missing minutes', () => {
  const breakout=bars.map((bar,index)=>index===bars.length-1?{...bar,volume:200}:bar);
  const markers=actionHistory(instrument,breakout,now);
  assert.equal(markers.at(-1).type,'buy');
  const future={...breakout.at(-1),time:Math.floor(now/MINUTE)*MINUTE,close:500,high:501,volume:99999};
  assert.deepEqual(actionHistory(instrument,[...breakout,future],now),markers);
  assert.equal(analyze(instrument,quote,breakout.filter((_,i)=>i!==breakout.length-2),now).action,null);
});

test('validation rejects impossible prices, future timestamps and unfinished candles', () => {
  const input = { id: instrument.id, ...quote, bars };
  assert.equal(validateUpdate(input, now).bars.length, 40);
  assert.throws(() => validateUpdate({ ...input, price: -1 }, now));
  assert.throws(() => validateUpdate({ ...input, timestamp: now + MINUTE }, now));
  assert.throws(() => validateUpdate({ ...input, bars: [{ ...bars[0], high: 1 }] }, now));
  assert.throws(() => validateUpdate({ ...input, bars: [{ ...bars[0], time: Math.floor(now / MINUTE) * MINUTE }] }, now));
});

test('live mode starts empty, merges bar corrections, and rejects older quotes', () => {
  const feed = new Feed({ mode: 'live', now });
  assert.ok(feed.snapshot(now).instruments.every(item => item.quote === null && item.strategy.levels === null));
  feed.apply(validateUpdate({ id: instrument.id, ...quote, bars }, now));
  feed.apply(validateUpdate({ id: instrument.id, ...quote, bars: [{ ...bars.at(-1), volume: 222 }] }, now));
  assert.equal(feed.states.get(instrument.id).bars.length, 40);
  assert.equal(feed.states.get(instrument.id).bars.at(-1).volume, 222);
  assert.throws(() => feed.apply({ id: instrument.id, ...quote, timestamp: now - 1, bars: [] }), /乱序/);
});
