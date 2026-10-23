import { analyze } from '/strategy.js';

const $ = selector => document.querySelector(selector);
const marketNames = { cn: 'A 股市场', futures: '国内商品期货', us: '美股市场' };
const marketZones = { cn: 'Asia/Shanghai', futures: 'Asia/Shanghai', us: 'America/New_York' };
const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const readStored = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const storedStars = readStored('barry-stars', ['SH.510300', 'SHFE.au', 'US.SPY']);
const state = { snapshot: null, receivedAt: 0, connected: false, lastEventAt: performance.now(), market: 'all', query: '', selected: readStored('barry-selected', 'SH.510300'), starred: new Set(Array.isArray(storedStars) ? storedStars : []) };
const save = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private browsing may disable storage. */ } };
const time = (value, date = false, zone = 'Asia/Shanghai') => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: zone, ...(date ? { month: '2-digit', day: '2-digit' } : {}), hour: '2-digit', minute: '2-digit', second: date ? '2-digit' : undefined, hour12: false }).format(value) : '—';
const now = () => state.snapshot ? state.snapshot.serverTime + (performance.now() - state.receivedAt) : Date.now();
const fresh = item => Boolean(state.connected && !item.dataIssue && item.quote && Number.isFinite(item.quote.timestamp) && item.quote.timestamp <= now() + 10_000 && now() - item.quote.timestamp <= 90_000);
const strategy = item => {
  if (state.connected && item.dataIssue) return { direction: 'waiting', label: item.quote ? '数据待核对' : '等待行情', reason: item.dataIssue, levels: null, barTime: null };
  if (state.connected && item.quote && !Number.isFinite(item.quote.timestamp)) return { direction: 'waiting', label: '行情时间未确认', reason: '报价日期或时间尚未核实，暂停策略判断；读取时间不能替代行情时间。', levels: null, barTime: null };
  return fresh(item) ? item.strategy : { direction: 'stale', label: state.connected ? (item.quote ? '行情已过期' : '等待行情') : '连接中断', reason: state.connected ? (item.quote ? '超过 90 秒未更新，暂停策略判断；休市也可能出现此状态。' : '尚未收到该品种的外部行情。') : '与行情看板的连接已中断，恢复前暂停策略判断。', levels: null, barTime: null };
};
const digits = item => (String(item.tick).split('.')[1] || '').length;
const price = (value, item) => value == null ? '—' : new Intl.NumberFormat('en-US', { minimumFractionDigits: digits(item), maximumFractionDigits: digits(item) }).format(Math.round(value / item.tick) * item.tick);
const change = item => item.quote ? (item.quote.price / item.quote.previousClose - 1) * 100 : null;
const pct = value => value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
const color = value => value == null || Math.abs(value) < 0.005 ? 'flat' : value > 0 ? 'up' : 'down';
const compact = value => value == null ? '—' : new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
const list = () => (state.snapshot?.instruments || []).filter(item => (state.market === 'all' || (state.market === 'starred' ? state.starred.has(item.id) : item.market === state.market)) && `${item.name} ${item.symbol} ${item.id} ${item.kind}`.toLowerCase().includes(state.query));

function renderOverview() {
  $('#overview').innerHTML = Object.entries(marketNames).map(([market, title], index) => {
    const items = state.snapshot.instruments.filter(item => item.market === market);
    const active = items.filter(fresh);
    const average = active.length ? active.reduce((sum, item) => sum + change(item), 0) / active.length : null;
    const up = active.filter(item => change(item) >= 0).length;
    return `<button class="market-card ${state.market === market ? 'selected' : ''}" data-overview="${market}"><div class="market-card-top"><span class="market-icon">${['沪', '期', 'US'][index]}</span><span>${title}</span><span class="market-tag">${market === 'us' ? 'USD' : 'CNY'}</span></div><div class="market-card-value ${color(average)}">${pct(average)}<span>样本平均涨跌</span></div><div class="market-card-bottom"><span>${active.length} / ${items.length} 个品种已更新</span><span><b class="up">${up} 涨</b> <b class="down">${active.length - up} 跌</b></span></div></button>`;
  }).join('');
}

function renderTable() {
  const items = list();
  $('#instrument-count').textContent = items.length;
  $('#quote-body').innerHTML = items.map(item => {
    const signal = strategy(item);
    return `<tr class="${state.selected === item.id ? 'selected' : ''} ${fresh(item) ? '' : 'stale-row'}"><td><button class="star ${state.starred.has(item.id) ? 'starred' : ''}" data-star="${item.id}" aria-label="${state.starred.has(item.id) ? '移除' : '添加'}自选 ${escape(item.name)}" aria-pressed="${state.starred.has(item.id)}">${state.starred.has(item.id) ? '★' : '☆'}</button></td><td><button class="instrument-button" data-select="${item.id}"><strong>${escape(item.name)}</strong><span>${escape(item.symbol)} <i>${item.kind}</i></span></button></td><td class="number quote-price ${color(change(item))}">${price(item.quote?.price, item)}<span>${item.currency}${fresh(item) ? '' : ' · 无新行情'}</span></td><td class="number"><span class="change-chip ${color(change(item))}">${pct(change(item))}</span></td><td><span class="signal-label ${signal.direction}"><span class="signal-dot"></span>${signal.label}</span></td></tr>`;
  }).join('');
  $('#empty-list').classList.toggle('hidden', items.length > 0);
}

function drawChart(item) {
  const bars = item.bars.slice(-60);
  if (bars.length < 2) { $('#chart').innerHTML = '<div class="chart-empty">收到分钟 K 线后显示走势</div>'; $('#chart-axis').innerHTML = '<span>—</span><span>—</span><span>—</span>'; return; }
  const values = bars.map(bar => bar.close);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const padding = Math.max((max - min) * 0.18, item.tick * 3);
  const lower = min - padding;
  const upper = max + padding;
  const y = value => 160 - (value - lower) / (upper - lower) * 140;
  const span = bars.at(-1).time - bars[0].time;
  const x = bar => 8 + (bar.time - bars[0].time) / span * 440;
  const segments = [];
  bars.forEach((bar, index) => {
    if (index === 0 || bar.time - bars[index - 1].time !== 60_000) segments.push([]);
    segments.at(-1).push({ x: x(bar).toFixed(2), y: y(bar.close).toFixed(2) });
  });
  const stroke = values.at(-1) >= values[0] ? '#ef8c7f' : '#78cbb0';
  const paths = segments.map(points => {
    const line = `M${points.map(point => `${point.x},${point.y}`).join(' L')}`;
    return `<path d="${line} L${points.at(-1).x},175 L${points[0].x},175 Z" fill="url(#chart-fill)"/><path d="${line}" stroke="${stroke}" fill="none" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  }).join('');
  $('#chart').innerHTML = `<svg viewBox="0 0 520 180" aria-hidden="true"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${stroke}" stop-opacity="0.18"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>${[35, 90, 145].map(line => `<line x1="8" y1="${line}" x2="448" y2="${line}" class="chart-grid"/><text x="458" y="${line + 4}" class="chart-axis-label">${price(upper - (line - 20) / 140 * (upper - lower), item)}</text>`).join('')}${paths}<circle cx="448" cy="${y(values.at(-1)).toFixed(2)}" r="3.5" fill="${stroke}"/></svg>`;
  $('#chart-axis').innerHTML = `<span>${time(bars[0].time)}</span><span>${time(bars[Math.floor(bars.length / 2)].time)}</span><span>${time(bars.at(-1).time)} 上海</span>`;
}

function renderDetail() {
  const item = state.snapshot.instruments.find(item => item.id === state.selected) || state.snapshot.instruments[0];
  if (!item) return;
  state.selected = item.id;
  const signal = strategy(item);
  $('#detail-symbol').textContent = `${item.id} / ${item.currency}`;
  $('#detail-name').textContent = item.name;
  $('#detail-kind').textContent = item.continuous ? '连续合约 · 仅观察' : item.kind;
  $('#detail-price').textContent = price(item.quote?.price, item);
  $('#detail-change').textContent = pct(change(item));
  $('#detail-change').className = `change ${color(change(item))}`;
  $('#chart-caption').textContent = state.snapshot.mode === 'demo' ? '模拟走势' : `${Math.min(item.bars.length, 60)} 根有效 K 线 · 缺失处断开`;
  drawChart(item);
  $('#detail-stats').innerHTML = [['前收盘', price(item.quote?.previousClose, item)], ['成交量 · 源单位', compact(item.quote?.volume)], ['行情时间 · 上海', item.quote?.timeLabel || time(item.quote?.timestamp)], item.quote?.receivedAt ? ['读取时间 · 上海', time(item.quote.receivedAt, true)] : [item.market === 'us' ? '行情时间 · 纽约' : '计价货币', item.market === 'us' ? time(item.quote?.timestamp, false, marketZones.us) : item.currency]].map(([label, value]) => `<div><span>${label}</span><strong>${escape(value)}</strong></div>`).join('');
  $('#strategy-label').textContent = signal.label;
  $('#strategy-label').className = signal.direction;
  $('#strategy-reason').textContent = signal.reason;
  $('#strategy-levels').innerHTML = signal.levels ? `<div class="levels">${[['参考收盘价', signal.levels.reference], ['趋势失效观察', signal.levels.invalidation], ['延伸观察位', signal.levels.observation]].map(([label, value]) => `<div><span>${label}</span><strong>${price(value, item)}</strong></div>`).join('')}</div><p class="level-note">${signal.levels.basis}</p>` : '';
  $('#strategy-time').textContent = signal.barTime ? `基于 ${time(signal.barTime + 60_000)} 收盘的 1 分钟 K 线 · 非收益预测` : '数据不足或过期时，不生成价格区间';
  $('#market-rule').textContent = item.rule;
}

function renderSignals() {
  const items = state.snapshot.instruments.filter(item => ['bullish', 'bearish'].includes(strategy(item).direction)).slice(0, 4);
  $('#signal-grid').innerHTML = items.length ? items.map(item => {
    const signal = strategy(item);
    return `<button class="signal-card" data-select="${item.id}"><div class="signal-card-top"><span class="signal-arrow ${signal.direction}">${signal.direction === 'bullish' ? '↗' : '↘'}</span><span class="signal-label ${signal.direction}">${signal.label}</span><span class="small-label">1 MIN</span></div><h3>${escape(item.name)} <span>${escape(item.symbol)}</span></h3><p>${escape(signal.reason)}</p><span class="small-label">${time(signal.barTime + 60_000)} 收盘计算 ${state.snapshot.mode === 'demo' ? '· 模拟' : ''}</span></button>`;
  }).join('') : '<div class="empty">暂无可用趋势信号。等待足够的新鲜行情和已收盘 K 线。</div>';
}

function render() {
  if (!state.snapshot) return;
  const demo = state.snapshot.mode === 'demo';
  const disconnected = !state.connected;
  $('#data-banner').classList.toggle('live-banner', !demo && !disconnected);
  $('#data-banner').innerHTML = `<span class="banner-icon">${disconnected ? '!' : demo ? 'D' : '↗'}</span><div><strong>${disconnected ? '连接已中断 · 策略判断已暂停' : demo ? '模拟体验模式 · 不是实际行情' : '外部行情模式 · 请核对数据服务的市场权限和延迟'}</strong><span>${disconnected ? '画面保留最近收到的数据，恢复连接后自动更新。' : demo ? '所有价格均由程序生成，仅用于体验界面与验证规则；接入你的行情服务后才能跟踪真实市场。' : '显示源数据时间；超过 90 秒未更新的品种停止策略判断。行情接入不等于交易授权。'}</span></div>`;
  if (!demo && !disconnected && state.snapshot.status?.message) {
    $('#data-banner div > span').textContent = state.snapshot.status.message;
  }
  $('#source-label').textContent = state.snapshot.source;
  renderOverview(); renderTable(); renderDetail(); renderSignals();
}

function setMarket(market) {
  state.market = market;
  document.querySelectorAll('[data-market]').forEach(button => { const active = button.dataset.market === market; button.classList.toggle('active', active); button.setAttribute('aria-pressed', active); });
  render();
}
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('#toast').classList.add('hidden'), 3000);
}

document.addEventListener('click', event => {
  const star = event.target.closest('[data-star]');
  if (star) { const id = star.dataset.star; if (state.starred.has(id)) state.starred.delete(id); else state.starred.add(id); save('barry-stars', [...state.starred]); render(); return; }
  const selection = event.target.closest('[data-select]');
  if (selection) { state.selected = selection.dataset.select; save('barry-selected', state.selected); render(); return; }
  const market = event.target.closest('[data-market], [data-overview]');
  if (market) setMarket(market.dataset.market || market.dataset.overview);
});
$('#search').addEventListener('input', event => { state.query = event.target.value.trim().toLowerCase(); if (state.snapshot) renderTable(); });
$('#watch-nav').addEventListener('click', () => setMarket('starred'));
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName) && !$('#help-dialog').open) { event.preventDefault(); $('#search').focus(); } });
$('#help-button').addEventListener('click', () => $('#help-dialog').showModal());
$('#close-help').addEventListener('click', () => $('#help-dialog').close());
$('#help-done').addEventListener('click', () => $('#help-dialog').close());
$('#export-button').addEventListener('click', () => {
  if (!state.snapshot) { toast('行情还未加载，请稍后。'); return; }
  const rows = [['模式', '品种', '代码', '货币', '最新价', '前收盘', '涨跌幅(%)', '行情时间(UTC)', '策略状态', '规则说明']];
  list().forEach(item => rows.push([state.snapshot.mode === 'demo' ? '模拟数据' : '外部接口', item.name, item.id, item.currency, item.quote?.price ?? '', item.quote?.previousClose ?? '', change(item)?.toFixed(4) ?? '', Number.isFinite(item.quote?.timestamp) ? new Date(item.quote.timestamp).toISOString() : '', strategy(item).label, strategy(item).reason]));
  const csv = '\uFEFF' + rows.map(row => row.map(value => `"${String(value).replace(/"/g, '""')}"`).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `barry-${state.snapshot.mode}-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('已导出当前筛选下的观察记录。');
});

function setConnection(connected) {
  state.connected = connected;
  $('#connection-dot').classList.toggle('connected', connected);
  $('#connection-text').textContent = connected ? '行情通道已连接' : '连接中断 · 正在重连';
}
let events;
function connect() {
  state.lastEventAt = performance.now();
  events = new EventSource('/api/events');
  events.onopen = () => { state.lastEventAt = performance.now(); };
  events.onmessage = event => {
    try {
      state.snapshot = JSON.parse(event.data);
      if (state.snapshot.computeStrategies) {
        state.snapshot.instruments.forEach(item => {
          item.strategy = analyze(item, item.quote, item.bars, state.snapshot.serverTime);
        });
      }
      state.receivedAt = performance.now();
      state.lastEventAt = state.receivedAt;
      setConnection(true);
      render();
    } catch { setConnection(false); render(); toast('行情数据格式异常，请刷新后重试。'); }
  };
  events.onerror = () => { setConnection(false); render(); };
}
connect();
let freshnessKey = '';
setInterval(() => {
  $('#clock').textContent = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(Date.now());
  if (performance.now() - state.lastEventAt > 15_000) {
    setConnection(false);
    render();
    events.close();
    connect();
  }
  if (state.snapshot) {
    const key = state.snapshot.instruments.map(item => fresh(item) ? '1' : '0').join('');
    if (key !== freshnessKey) { freshnessKey = key; render(); }
  }
}, 1000);
