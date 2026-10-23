import { analyze, actionHistory } from '/strategy.js';

const $ = selector => document.querySelector(selector);
const marketNames = { cn: 'A 股市场', futures: '国内商品期货', us: '美股市场' };
const marketZones = { cn: 'Asia/Shanghai', futures: 'Asia/Shanghai', us: 'America/New_York' };
const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const readStored = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const state = { snapshot: null, receivedAt: 0, connected: false, lastEventAt: performance.now(), market: 'all', selected: readStored('barry-selected', 'SH.510300') };
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
const list = () => (state.snapshot?.instruments || []).filter(item => state.market === 'all' || item.market === state.market);

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
    const action = signal.action;
    return `<tr class="${state.selected === item.id ? 'selected' : ''} ${fresh(item) ? '' : 'stale-row'}"><td><button class="remove-watch" data-remove="${escape(item.id)}" aria-label="移除自选 ${escape(item.name)}">×</button></td><td><button class="instrument-button" data-select="${escape(item.id)}"><strong>${escape(item.name)}</strong><span>${escape(item.symbol)} <i>${escape(item.kind)}</i></span></button></td><td class="number quote-price ${color(change(item))}">${price(item.quote?.price, item)}<span>${item.currency}${fresh(item) ? '' : ' · 无新行情'}</span></td><td class="number"><span class="change-chip ${color(change(item))}">${pct(change(item))}</span></td><td class="action-cell" aria-label="${action ? action.label : '未触发买卖标记'}">${action ? `<span class="trade-tag ${action.type}">${action.label}</span>` : ''}</td></tr>`;
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
  const markers = actionHistory(item,item.bars,state.snapshot.serverTime).filter(marker=>marker.barTime>=bars[0].time).map(marker=>{
    const cx=x({time:marker.barTime}).toFixed(2), cy=Math.max(12,Math.min(166,y(marker.price)+(marker.type==='buy'?17:-17))).toFixed(2);
    return `<g class="chart-marker ${marker.type}"><title>${marker.label}规则触发于${time(marker.time)}，历史信号</title><circle cx="${cx}" cy="${cy}" r="8"/><text x="${cx}" y="${Number(cy)+3}">${marker.type==='buy'?'B':'S'}</text></g>`;
  }).join('');
  $('#chart').innerHTML = `<svg viewBox="0 0 520 180" aria-hidden="true"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${stroke}" stop-opacity="0.18"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>${[35, 90, 145].map(line => `<line x1="8" y1="${line}" x2="448" y2="${line}" class="chart-grid"/><text x="458" y="${line + 4}" class="chart-axis-label">${price(upper - (line - 20) / 140 * (upper - lower), item)}</text>`).join('')}${paths}<circle cx="448" cy="${y(values.at(-1)).toFixed(2)}" r="3.5" fill="${stroke}"/>${markers}</svg>`;
  $('#chart-axis').innerHTML = `<span>${time(bars[0].time)}</span><span>${time(bars[Math.floor(bars.length / 2)].time)}</span><span>${time(bars.at(-1).time)} 上海</span>`;
}

function renderDetail() {
  const item = state.snapshot.instruments.find(item => item.id === state.selected) || state.snapshot.instruments[0];
  if (!item) {
    $('#detail-name').textContent='搜索并添加一个品种'; $('#detail-symbol').textContent='开始建立你的自选';
    $('#detail-price').textContent='—'; $('#detail-change').textContent=''; $('#detail-kind').textContent='';
    $('#selected-action').className='trade-tag hidden'; $('#strategy-label').classList.add('hidden');
    $('#chart').innerHTML='<div class="chart-empty">添加自选后查看图表与买卖信号</div>';
    $('#chart-axis').innerHTML=''; $('#detail-stats').innerHTML=''; $('#strategy-levels').innerHTML='';
    $('#strategy-reason').textContent='在上方搜索名称或完整证券代码。'; $('#strategy-time').textContent=''; $('#market-rule').textContent='';return;
  }
  state.selected = item.id;
  const signal = strategy(item);
  $('#detail-symbol').textContent = `${item.id} / ${item.currency}`;
  $('#detail-name').textContent = item.name;
  $('#detail-kind').textContent = item.continuous ? '连续合约 · 仅观察' : item.kind;
  $('#detail-price').textContent = price(item.quote?.price, item);
  $('#detail-change').textContent = pct(change(item));
  $('#detail-change').className = `change ${color(change(item))}`;
  $('#selected-action').textContent=signal.action?.label || '';
  $('#selected-action').className=`trade-tag ${signal.action?.type || ''} ${signal.action?'':'hidden'}`;
  $('#chart-caption').textContent = state.snapshot.mode === 'demo' ? '模拟走势' : `${Math.min(item.bars.length, 60)} 根有效 K 线 · 缺失处断开`;
  drawChart(item);
  $('#detail-stats').innerHTML = [['前收盘', price(item.quote?.previousClose, item)], ['成交量 · 源单位', compact(item.quote?.volume)], ['行情时间 · 上海', item.quote?.timeLabel || time(item.quote?.timestamp)], item.quote?.receivedAt ? ['读取时间 · 上海', time(item.quote.receivedAt, true)] : [item.market === 'us' ? '行情时间 · 纽约' : '计价货币', item.market === 'us' ? time(item.quote?.timestamp, false, marketZones.us) : item.currency]].map(([label, value]) => `<div><span>${label}</span><strong>${escape(value)}</strong></div>`).join('');
  $('#strategy-label').textContent = signal.action?.label || '';
  $('#strategy-label').className = signal.action ? signal.action.type : 'hidden';
  $('#strategy-reason').textContent = signal.action ? `${signal.action.reason} ${signal.action.note}` : ['bullish','bearish','neutral'].includes(signal.direction) ? '当前未同时满足趋势、突破和放量条件，买卖标记留空。' : signal.reason;
  $('#strategy-levels').innerHTML = signal.action && signal.levels ? `<div class="levels">${[['触发收盘价', signal.levels.reference], ['趋势失效观察', signal.levels.invalidation], ['延伸观察位', signal.levels.observation]].map(([label, value]) => `<div><span>${label}</span><strong>${price(value, item)}</strong></div>`).join('')}</div><p class="level-note">${escape(signal.levels.basis)}</p>` : '';
  $('#strategy-time').textContent = signal.barTime ? `基于 ${time(signal.barTime + 60_000)} 收盘的 1 分钟 K 线 · 非收益预测` : '数据不足或过期时，不生成价格区间';
  $('#market-rule').textContent = item.rule;
}

function renderSignals() {
  const items = state.snapshot.instruments.filter(item => strategy(item).action).slice(0, 4);
  $('#signal-grid').innerHTML = items.length ? items.map(item => {
    const signal = strategy(item);
    return `<button class="signal-card" data-select="${escape(item.id)}"><div class="signal-card-top"><span class="trade-tag ${signal.action.type}">${signal.action.label}</span><span class="small-label">1 MIN</span></div><h3>${escape(item.name)} <span>${escape(item.symbol)}</span></h3><p>${escape(signal.action.reason)}</p><span class="small-label">${time(signal.action.time)} 收盘触发 ${state.snapshot.mode === 'demo' ? '· 模拟' : ''}</span></button>`;
  }).join('') : '<div class="empty">当前没有同时满足规则的买卖信号。</div>';
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
  $('#directory-status').textContent=state.snapshot.directoryStatus || '输入代码或名称查找证券，点击添加开始读取行情。';
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

document.addEventListener('click', async event => {
  const add=event.target.closest('[data-add]');
  if(add) {
    add.disabled=true;
    try { const data=await api('/api/watchlist',{code:add.dataset.add}); state.selected=data.instrument.id;save('barry-selected',state.selected);++searchGeneration;clearTimeout(searchTimer);searchController?.abort();$('#search').value='';$('#search-results').classList.add('hidden');await refresh();setMarket('all');toast('已添加自选，正在读取行情。'); } catch(error){toast(error.message);add.disabled=false;} return;
  }
  const remove=event.target.closest('[data-remove]');
  if(remove){try{await api('/api/watchlist/remove',{id:remove.dataset.remove});await refresh();toast('已移除自选。');}catch(error){toast(error.message);}return;}
  const selection = event.target.closest('[data-select]');
  if (selection) { state.selected = selection.dataset.select; save('barry-selected', state.selected); render(); if(innerWidth<640)document.querySelector('.detail-panel').scrollIntoView({behavior:'smooth',block:'start'});return; }
  const market = event.target.closest('[data-market], [data-overview]');
  if (market) setMarket(market.dataset.market || market.dataset.overview);
});
let searchTimer,searchController,searchGeneration=0;
async function search(query,generation){
  if(!query){$('#search-results').classList.add('hidden');return;}
  searchController?.abort();searchController=new AbortController();
  try{
    const response=await fetch(`/api/search?q=${encodeURIComponent(query)}`,{signal:searchController.signal});
    if(!response.ok)throw Error('搜索服务暂不可用');
    const data=await response.json();if(generation!==searchGeneration)return;
    $('#search-results').classList.remove('hidden');
    $('#search-results').innerHTML=(data.results||[]).map(item=>`<div class="search-result"><div><strong>${escape(item.name)}</strong><span>${escape(item.windCode||item.id)} · ${escape(item.kind)} · ${item.currency}</span></div><button class="button ${item.added?'secondary':''}" data-add="${escape(item.id)}" ${item.added?'disabled':''}>${item.added?'已添加':'+ 添加'}</button></div>`).join('')+`<p class="search-note">${escape(data.pending?'Wind 正在校验代码，请稍候……':data.message||'名称目录中没有匹配项，可尝试完整代码或导入目录。')}</p>`;
    if(data.pending)searchTimer=setTimeout(()=>search(query,generation),1000);
  }catch(error){if(error.name!=='AbortError'&&generation===searchGeneration){$('#search-results').classList.remove('hidden');$('#search-results').textContent=error.message;}}
}
$('#search').addEventListener('input',event=>{clearTimeout(searchTimer);const generation=++searchGeneration;const query=event.target.value.trim();searchTimer=setTimeout(()=>search(query,generation),300);});
$('#watch-nav').addEventListener('click', () => {setMarket('all');document.querySelector('.watchlist-panel').scrollIntoView({behavior:'smooth'});});
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName) && !$('#help-dialog').open) { event.preventDefault(); $('#search').focus(); } });
$('#help-button').addEventListener('click', () => {$('#us-quote-zone').value=state.snapshot?.settings?.usQuoteZone||'unknown';$('#us-bar-zone').value=state.snapshot?.settings?.usBarZone||'unknown';$('#help-dialog').showModal();});
$('#close-help').addEventListener('click', () => $('#help-dialog').close());
$('#help-done').addEventListener('click', () => $('#help-dialog').close());
$('#save-settings').addEventListener('click',async()=>{
  try{if(state.snapshot?.provider!=='wind-local')throw Error('请在 Wind 本机版设置行情时间口径。');await api('/api/settings',{usQuoteZone:$('#us-quote-zone').value,usBarZone:$('#us-bar-zone').value});$('#settings-message').textContent='已保存，正在重新读取美股行情。';await refresh();}catch(error){$('#settings-message').textContent=error.message;}
});
$('#import-button').addEventListener('click',()=>$('#catalog-file').click());
$('#catalog-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(state.snapshot?.provider!=='wind-local')throw Error('名称目录导入请在 Wind 本机版使用。');
    if(file.size>1_500_000)throw Error('目录文件请控制在 1.5 MB 以内。');
    const bytes=await file.arrayBuffer();let text=new TextDecoder('utf-8').decode(bytes);
    if(text.includes('\ufffd'))text=new TextDecoder('gb18030').decode(bytes);
    const data=await api('/api/catalog',{csv:text});toast(`已导入 ${data.imported} 条名称，可在上方搜索。`);
    if($('#search').value.trim())search($('#search').value.trim(),++searchGeneration);
  }catch(error){toast(error.message);}finally{event.target.value='';}
});
$('#export-button').addEventListener('click', () => {
  if (!state.snapshot) { toast('行情还未加载，请稍后。'); return; }
  const rows = [['模式', '品种', '代码', '货币', '最新价', '前收盘', '涨跌幅(%)', '行情时间(UTC)', '买卖标记', '规则说明']];
  list().forEach(item => rows.push([state.snapshot.mode === 'demo' ? '模拟数据' : '外部接口', item.name, item.id, item.currency, item.quote?.price ?? '', item.quote?.previousClose ?? '', change(item)?.toFixed(4) ?? '', Number.isFinite(item.quote?.timestamp) ? new Date(item.quote.timestamp).toISOString() : '', strategy(item).action?.label||'', strategy(item).action?.reason||strategy(item).reason]));
  const csv = '\uFEFF' + rows.map(row => row.map(value => {const text=String(value);return `"${(typeof value==='string'&&/^[=+\-@\t\r]/.test(text)?"'"+text:text).replace(/"/g, '""')}"`;}).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `barry-${state.snapshot.mode}-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('已导出当前筛选下的观察记录。');
});

function setConnection(connected) {
  state.connected = connected;
  $('#connection-dot').classList.toggle('connected', connected);
  $('#connection-text').textContent = connected ? '行情通道已连接' : '连接中断 · 正在重连';
}
async function api(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败，请重试。');return data;
}
function acceptSnapshot(snapshot,connected=false){
  state.snapshot=snapshot;
  if(snapshot.computeStrategies)snapshot.instruments.forEach(item=>{item.strategy=analyze(item,item.quote,item.bars,snapshot.serverTime);});
  if(!snapshot.instruments.some(item=>item.id===state.selected))state.selected=snapshot.instruments[0]?.id||'';
  state.receivedAt=performance.now();
  if(connected){state.lastEventAt=state.receivedAt;setConnection(true);}
  render();
}
async function refresh(){const response=await fetch('/api/snapshot');if(!response.ok)throw Error('行情快照读取失败');acceptSnapshot(await response.json());}
let events;
function connect() {
  state.lastEventAt = performance.now();
  events = new EventSource('/api/events');
  events.onopen = () => { state.lastEventAt = performance.now(); };
  events.onmessage = event => {
    try {
      acceptSnapshot(JSON.parse(event.data),true);
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
