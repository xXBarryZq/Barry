export const MINUTE = 60_000;
export const STALE_AFTER = 90_000;

export function ema(values, period) {
  const alpha = 2 / (period + 1);
  return values.slice(1).reduce((previous, value) => alpha * value + (1 - alpha) * previous, values[0]);
}

export function analyze(instrument, quote, bars, now = Date.now()) {
  const base = { direction: 'waiting', label: '等待行情', reason: '行情接入后，使用已收盘的 1 分钟 K 线计算。', period: '1 分钟', barTime: null, levels: null, action: null };
  if (!quote) return base;
  if (!Number.isFinite(quote.timestamp) || quote.timestamp > now + 10_000) return { ...base, label: '行情时间未确认', reason: '报价日期或时间尚未核实，暂停策略判断。查询时间不能替代源行情时间。' };
  if (now - quote.timestamp > STALE_AFTER) return { ...base, direction: 'stale', label: '行情已过期', reason: '超过 90 秒未更新，暂停策略判断；休市时也可能出现此状态。' };
  const candidates = bars.filter(bar => bar.time + MINUTE <= now).slice(-120);
  const valid = bar => [bar.open, bar.high, bar.low, bar.close].every(value => Number.isFinite(value) && value > 0)
    && bar.low <= Math.min(bar.open, bar.close) && bar.high >= Math.max(bar.open, bar.close);
  const last = candidates.at(-1);
  if (!last) return { ...base, reason: '需要至少 21 根连续、有效且已收盘的 1 分钟 K 线，当前 0 根。' };
  if (!valid(last)) return { ...base, label: '分钟数据缺失', reason: '最新已收盘 K 线含缺失或无效价格，暂停策略判断；不以零或前值填补。' };
  if (now - (last.time + MINUTE) > 3 * MINUTE) return { ...base, direction: 'stale', label: 'K 线已过期', reason: '最新已收盘 K 线超过 3 分钟，暂停策略判断。', barTime: last.time };
  let start = candidates.length - 1;
  while (start > 0 && valid(candidates[start - 1]) && candidates[start].time - candidates[start - 1].time === MINUTE) start--;
  const closed = candidates.slice(start);
  if (closed.length < 21) return { ...base, label: '等待完整分钟数据', reason: `需要至少 21 根连续、有效且已收盘的 1 分钟 K 线，当前连续 ${closed.length} 根。缺失、时间间隔或休市会重新累计。` };
  const closes = closed.map(bar => bar.close);
  const fast = ema(closes, 5);
  const slow = ema(closes, 20);
  const trueRanges = closed.slice(1).map((bar, index) => Math.max(bar.high - bar.low, Math.abs(bar.high - closed[index].close), Math.abs(bar.low - closed[index].close)));
  const atr = trueRanges.slice(-14).reduce((a, b) => a + b, 0) / 14;
  const prior = closed.slice(-21, -1);
  const resistance = Math.max(...prior.map(bar => bar.high));
  const support = Math.min(...prior.map(bar => bar.low));
  const threshold = Math.max(atr * 0.15, instrument.tick);
  const direction = fast > slow + threshold ? 'bullish' : fast < slow - threshold ? 'bearish' : 'neutral';
  const volumes = prior.map(bar => bar.volume);
  const averageVolume = volumes.every(value => Number.isFinite(value) && value >= 0) ? volumes.reduce((sum, value) => sum + value, 0) / volumes.length : null;
  const volumeConfirmed = averageVolume > 0 && Number.isFinite(last.volume) && last.volume >= averageVolume * 1.2;
  const actionType = volumeConfirmed && direction === 'bullish' && last.close > resistance
    && Number.isFinite(quote.price) && quote.price > resistance ? 'buy'
    : volumeConfirmed && direction === 'bearish' && last.close < support
    && Number.isFinite(quote.price) && quote.price < support ? 'sell' : null;
  const action = actionType ? {
    type: actionType, label: actionType === 'buy' ? '买入' : '卖出', time: last.time + MINUTE,
    reason: actionType === 'buy' ? '均线趋势向上，收盘价突破前 20 分钟高点，成交量达到前 20 分钟均量的 1.2 倍。'
      : '均线趋势向下，收盘价跌破前 20 分钟低点，成交量达到前 20 分钟均量的 1.2 倍。',
    note: actionType === 'sell' && instrument.market !== 'futures' ? '供已有持仓参考，不表示可做空；A 股还需遵守 T+1。' : '基于分钟规则的参考信号，未经收益回测，不保证成交或收益。'
  } : null;
  const labels = { bullish: '趋势偏强', bearish: '趋势偏弱', neutral: '区间观察' };
  const reason = direction === 'bullish'
    ? `EMA5 高于 EMA20${last.close > resistance ? '，收盘价突破前 20 根 K 线高点' : '，尚待放量或突破确认'}。`
    : direction === 'bearish'
      ? `EMA5 低于 EMA20${last.close < support ? '，收盘价跌破前 20 根 K 线低点' : '，关注支撑与下行风险'}。`
      : '快慢均线接近，当前方向不明确，继续观察。';
  const distance = Math.max(2 * atr, instrument.tick * 2);
  const sign = direction === 'bearish' ? -1 : 1;
  const levels = direction === 'neutral' || !Number.isFinite(atr) || atr <= 0 ? null : {
    reference: last.close,
    invalidation: Math.max(instrument.tick, last.close - sign * distance),
    observation: Math.max(instrument.tick, last.close + sign * 2 * distance),
    basis: direction === 'bearish' && instrument.market !== 'futures' ? '偏弱风险观察，不代表可做空' : '以收盘价和 2×ATR 推算的观察区间，不是下单价格'
  };
  return { direction, label: labels[direction], reason, period: '1 分钟', barTime: last.time, fast, slow, atr, support, resistance, averageVolume, volumeConfirmed, levels, action };
}

export function actionHistory(instrument, bars, now = Date.now()) {
  const markers = [];
  for (let index = 20; index < bars.length; index++) {
    const bar = bars[index];
    const end = bar.time + MINUTE;
    if (end > now) continue;
    const result = analyze(instrument, { price: bar.close, timestamp: end }, bars.slice(0, index + 1), end);
    if (result.action) markers.push({ ...result.action, barTime: bar.time, price: bar.close });
  }
  return markers;
}
