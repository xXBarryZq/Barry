import { demoSwing } from './demo-swing.js';
import { instruments, publicInstrument } from './instruments.js';
import { analyze, MINUTE, STALE_AFTER } from './strategy.js';

const demoCatalog = [
  {id:'SH.601318',symbol:'601318',name:'中国平安',market:'cn',kind:'股票',currency:'CNY',base:50,tick:.01,rule:'A 股股票 T+1；卖出标记供已有持仓参考。'},
  {id:'SZ.000001',symbol:'000001',name:'平安银行',market:'cn',kind:'股票',currency:'CNY',base:12,tick:.01,rule:'A 股股票 T+1；卖出标记供已有持仓参考。'},
  {id:'SH.510050',symbol:'510050',name:'上证50 ETF',market:'cn',kind:'ETF',currency:'CNY',base:3,tick:.001,rule:'普通股票 ETF 通常 T+1。'},
  {id:'US.TSLA',symbol:'TSLA',name:'Tesla 特斯拉',market:'us',kind:'股票',currency:'USD',base:250,tick:.01,rule:'美元计价；注意账户交易权限及行情时间。'}
];

export function validateUpdate(update, now = Date.now()) {
  if (!update || typeof update !== 'object' || !instruments.some(item => item.id === update.id)) throw new Error('未知品种 id');
  const positive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
  if (!positive(update.price) || !positive(update.previousClose)) throw new Error('price 和 previousClose 必须为正数');
  if (!Number.isSafeInteger(update.timestamp) || update.timestamp <= 0 || update.timestamp > now + 10_000) throw new Error('timestamp 必须是有效的 UTC 毫秒时间戳，不能在未来');
  if (!Number.isFinite(update.volume) || update.volume < 0) throw new Error('volume 必须为非负数');
  const bars = update.bars ?? [];
  if (!Array.isArray(bars) || bars.length > 120) throw new Error('每次最多传入 120 根 K 线');
  for (const bar of bars) {
    if (!bar || !Number.isSafeInteger(bar.time) || bar.time <= 0 || bar.time % MINUTE !== 0 || bar.time + MINUTE > now) throw new Error('bars 只接受已收盘的 1 分钟 K 线；time 须对齐分钟');
    if (![bar.open, bar.high, bar.low, bar.close].every(positive) || bar.low > Math.min(bar.open, bar.close) || bar.high < Math.max(bar.open, bar.close) || bar.low > bar.high) throw new Error('K 线 OHLC 无效');
    if (!Number.isFinite(bar.volume) || bar.volume < 0) throw new Error('K 线 volume 无效');
  }
  return { id: update.id, timestamp: update.timestamp, price: update.price, previousClose: update.previousClose, volume: update.volume, bars: bars.map(({ time, open, high, low, close, volume }) => ({ time, open, high, low, close, volume })) };
}

export class Feed {
  constructor({ mode = 'demo', now = Date.now() } = {}) {
    this.mode = mode;
    this.catalog = new Map([...instruments,...demoCatalog].map(item => [item.id,item]));
    this.active = new Set(instruments.map(item => item.id));
    this.states = new Map([...this.catalog.values()].map(instrument => [instrument.id, { quote: null, bars: [], forming: null }]));
    if (mode === 'demo') this.seed(now);
  }

  seed(now) {
    const minute = Math.floor(now / MINUTE) * MINUTE;
    let seed = 20261023;
    const random = () => ((seed = (1664525 * seed + 1013904223) >>> 0) / 4294967296);
    [...this.catalog.values()].forEach((instrument, index) => {
      const state = this.states.get(instrument.id);
      let price = instrument.base;
      for (let i = 100; i > 0; i--) {
        const open = price;
        price *= 1 + (random() - 0.48 + (index % 3 - 1) * 0.12) * 0.002;
        state.bars.push({ time: minute - i * MINUTE, open, high: Math.max(open, price) * (1 + random() * 0.0008), low: Math.min(open, price) * (1 - random() * 0.0008), close: price, volume: Math.floor(100 + random() * 8000) });
      }
      state.swing=demoSwing(instrument.base,now,index);
      state.quote = { timestamp: now, price, previousClose: instrument.base, volume: state.bars.reduce((sum, bar) => sum + bar.volume, 0) };
    });
  }

  tick(now = Date.now()) {
    if (this.mode !== 'demo') return;
    for (const instrument of this.catalog.values()) {
      const state = this.states.get(instrument.id);
      const time = Math.floor(now / MINUTE) * MINUTE;
      const price = Math.max(instrument.tick, state.quote.price * (1 + (Math.random() - 0.5) * 0.0005));
      const volume = Math.floor(10 + Math.random() * 100);
      if (state.forming && state.forming.time < time) {
        state.bars.push(state.forming);
        state.bars = state.bars.slice(-120);
        state.forming = null;
      }
      if (!state.forming) state.forming = { time, open: state.quote.price, high: price, low: price, close: price, volume: 0 };
      Object.assign(state.forming, { high: Math.max(state.forming.high, state.forming.open, price), low: Math.min(state.forming.low, state.forming.open, price), close: price, volume: state.forming.volume + volume });
      state.quote = { ...state.quote, timestamp: now, price, volume: state.quote.volume + volume };
    }
  }

  apply(update) {
    const state = this.states.get(update.id);
    if (state.quote && update.timestamp < state.quote.timestamp) throw new Error('拒绝乱序行情：timestamp 早于最新行情');
    const merged = new Map(state.bars.map(bar => [bar.time, bar]));
    update.bars.forEach(bar => merged.set(bar.time, bar));
    state.bars = [...merged.values()].sort((a, b) => a.time - b.time).slice(-120);
    const { id, bars, ...quote } = update;
    state.quote = quote;
  }

  search(query) {
    const q = query.trim().toLowerCase();
    const normalized = q.replace(/^(\d{6})\.(sh|sz|bj)$/, '$2.$1');
    return {results:[...this.catalog.values()].filter(item => q && `${item.name} ${item.symbol} ${item.id}`.toLowerCase().includes(normalized)).slice(0,15).map(item => ({...publicInstrument(item),added:this.active.has(item.id)})),pending:false,message:'模拟目录用于体验添加流程；Wind 本机版通过名称目录和代码校验查询。'};
  }

  add(id) {
    const item = this.catalog.get(id);
    if (!item) throw new Error('演示目录中没有该品种；请在 Wind 本机版查询完整代码。');
    this.active.add(id);
    return publicInstrument(item);
  }

  remove(id) { this.active.delete(id); }

  snapshot(now = Date.now()) {
    return { mode: this.mode, editableWatchlist:this.mode==='demo', directoryStatus:'模拟名称目录', source: this.mode === 'demo' ? '内置模拟行情 · 非真实市场数据' : '外部行情接口 · 授权与延迟由数据服务决定', serverTime: now, instruments: [...this.active].map(id => this.catalog.get(id)).map(instrument => {
      const state = this.states.get(instrument.id);
      return { ...publicInstrument(instrument), ...state.swing, quote: state.quote, stale: !state.quote || now - state.quote.timestamp > STALE_AFTER, bars: state.bars, strategy: analyze(instrument, state.quote, state.bars, now) };
    }) };
  }
}
