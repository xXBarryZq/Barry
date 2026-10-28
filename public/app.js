import { prepareSwing, currentSwing, PROFILES, validBar } from '/swing.js';
import { updatePosition } from '/positions.js';
import { AlertEngine, playChime } from '/alerts.js';
import { MarketConnection } from '/market-connection.js';
import { createContextPolicy } from '/context.js';

const $ = selector => document.querySelector(selector);
const marketNames = { cn: 'A 股市场', futures: '国内商品期货', us: '美股市场' };
const marketZones = { cn: 'Asia/Shanghai', futures: 'Asia/Shanghai', us: 'America/New_York' };
const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

const tipRow = (k, v, mod='') => v == null || v === '' ? '' : `<div class="tip-row${mod ? ` ${mod}` : ''}"><span class="tip-k">${k}</span><span class="tip-v">${escape(v)}</span></div>`;
const tipStack = rows => `<div class="tip-stack">${rows.filter(Boolean).join('')}</div>`;
function formatStrategyTips(item, signal) {
  const p = item?.swings?.[state.profile]?.prepared?.p || PROFILES[state.profile];
  const rule = `${p.label} · EMA${p.fast}/${p.slow} + 60m×${p.breakout} · 量≥1.2×`;
  const gapNeed = `日线EMA${p.fast}/${p.slow}方向 + 60m突破前${p.breakout}根 + 量≥1.2×`;
  if (!item) {
    return tipStack([tipRow('当前结论', '待选品种', 'tip-mute'), tipRow('怎么做', '在上方搜索名称或完整证券代码。')]);
  }
  if (signal.action) {
    const a = signal.action;
    const conclusion = a.type === 'buy' ? '偏多 · 买入参考'
      : a.type === 'sell' ? '偏空 · 卖出参考'
      : a.type === 'risk' ? '风险 · 观察减仓'
      : (a.label || '已触发');
    const mod = a.type === 'buy' ? 'tip-up' : a.type === 'sell' ? 'tip-down' : a.type === 'risk' ? 'tip-warn' : '';
    return tipStack([
      tipRow('当前结论', conclusion, mod),
      tipRow('为什么', a.reason || signal.reason || ''),
      tipRow('还差什么', '条件已触发；关注成交与持仓风险。', 'tip-mute'),
      tipRow('风险/注意', a.note || ''),
      tipRow('规则口径', rule, 'tip-mute'),
    ]);
  }
  const reason = signal.reason || '等待判断。';
  let conclusion = '观望';
  let mod = '';
  let gap = `还差：${gapNeed}`;
  if (/暂停|过期|中断|失败|未确认|未覆盖|暂不启用|数据处理/.test(reason)) {
    conclusion = '暂停';
    mod = 'tip-warn';
    gap = /期货/.test(reason) ? '期货口径验证前不启用波段提醒。' : '恢复有效行情、日线覆盖与已收盘60分钟后再判断。';
  } else if (/等待|未同时满足/.test(reason)) {
    conclusion = '观望';
    gap = `还差：${gapNeed}`;
  }
  return tipStack([
    tipRow('当前结论', conclusion, mod),
    tipRow('为什么', reason),
    tipRow('还差什么', gap),
    tipRow('规则口径', rule, 'tip-mute'),
  ]);
}
function formatSignalCardBody(signal) {
  if (!signal.action) return '';
  const a = signal.action;
  const conclusion = a.type === 'buy' ? '偏多' : a.type === 'sell' ? '偏空' : a.type === 'risk' ? '风险' : a.label;
  const mod = a.type === 'buy' ? 'tip-up' : a.type === 'sell' ? 'tip-down' : a.type === 'risk' ? 'tip-warn' : '';
  return tipStack([
    tipRow('结论', conclusion, mod),
    tipRow('为什么', a.reason || ''),
    a.note ? tipRow('注意', a.note, 'tip-mute') : '',
  ]);
}
const readStored = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const state = { profile:'short', processingIssue:'', transportIssue:'', snapshot: null, receivedAt: 0, connected: false, market: 'all', selected: readStored('barry-selected', 'SH.510300') };
const save = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private browsing may disable storage. */ } };
const time = (value, date = false, zone = 'Asia/Shanghai') => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: zone, ...(date ? { month: '2-digit', day: '2-digit' } : {}), hour: '2-digit', minute: '2-digit', second: date ? '2-digit' : undefined, hour12: false }).format(value) : '—';
const now = () => state.snapshot ? state.snapshot.serverTime + (performance.now() - state.receivedAt) : Date.now();
const fresh = item => Boolean(state.connected && !state.processingIssue && !item.dataIssue && item.quote && Number.isFinite(item.quote.timestamp) && item.quote.timestamp <= now() + 10_000 && now() - item.quote.timestamp <= 90_000);
const strategy = (item,mode=state.profile) => {
  const signal=item.swings?.[mode]?.current || {action:null,reason:'等待日线与60分钟历史。'};
  if(state.processingIssue)return {action:null,reason:state.processingIssue};
  return fresh(item)?signal:{action:null,reason:state.connected?'行情过期或数据待核对，已暂停。':'行情连接中断，已暂停。'};
};
const riskSettings=readStored('barry-swing-risk',{});
const positions=readStored('barry-swing-positions',{});
const dayInZone=(value,zone) => new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).format(value);
const analysisCache=new Map();
function computeSwings(snapshot) {
  for(const item of snapshot.instruments) {
    item.swings={};
    for(const mode of Object.keys(PROFILES)) {
      const cacheKey=`${item.id}:${mode}`;
      const version=JSON.stringify([item.swingRevision,item.hourlyBars?.length,item.hourlyBars?.at(-1),item.dailyBars?.length,item.dailyBars?.at(-1),riskSettings[mode]]);
      let cache=analysisCache.get(cacheKey);
      if(!cache||cache.version!==version){cache={version,prepared:prepareSwing(item,mode,riskSettings[mode])};analysisCache.set(cacheKey,cache);}
      const prepared=cache.prepared,current=currentSwing(item,prepared,snapshot.serverTime);
      const posKey=`${item.id}:${mode}`;
      let holding=null;
      if(!item.dataIssue&&item.market!=='futures'&&item.quote?.timestamp&&snapshot.serverTime-item.quote.timestamp<=90000) {
        holding=updatePosition(positions[posKey],item.quote,prepared.p,item.market,dayInZone(item.quote.timestamp,marketZones[item.market]),item.calendar||[]);
        if(holding.position)positions[posKey]=holding.position;
        if(holding.action){current.action=holding.action;current.barTime=holding.action.time;current.levels=null;}
      }
      item.swings[mode]={prepared,current,holding};
    }
  }
  save('barry-swing-positions',positions);
  for(const key of analysisCache.keys())if(!snapshot.instruments.some(item=>key.startsWith(item.id+':')))analysisCache.delete(key);
}
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
  const raw = (item.hourlyBars||[]).slice(-60);
  const bars = raw.filter(validBar);
  if(bars.length<2){$('#chart').innerHTML='<div class="chart-empty">等待60分钟历史</div>';$('#chart-axis').innerHTML='';return;}
  const values = bars.map(bar => bar.close);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const padding = Math.max((max - min) * 0.18, item.tick * 3);
  const lower = min - padding;
  const upper = max + padding;
  const y = value => 160 - (value - lower) / (upper - lower) * 140;
  const lastIndex = bars.length - 1;
  const xAt = index => 8 + index / lastIndex * 440;
  const points = bars.map((bar, index) => ({ x: xAt(index).toFixed(2), y: y(bar.close).toFixed(2) }));
  const stroke = values.at(-1) >= values[0] ? '#ef8c7f' : '#78cbb0';
  const line = `M${points.map(point => `${point.x},${point.y}`).join(' L')}`;
  const paths = `<path d="${line} L${points.at(-1).x},175 L${points[0].x},175 Z" fill="url(#chart-fill)"/><path d="${line}" stroke="${stroke}" fill="none" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  const markerX = barTime => {
    let best = 0, bestDelta = Math.abs(bars[0].time - barTime);
    for (let i = 1; i < bars.length; i++) {
      const delta = Math.abs(bars[i].time - barTime);
      if (delta < bestDelta) { best = i; bestDelta = delta; }
    }
    return xAt(best);
  };
  const markers = (item.swings?.[state.profile]?.prepared.signals||[]).filter(Boolean).filter(marker=>marker.barTime>=bars[0].time).map(marker=>{
    const cx=markerX(marker.barTime).toFixed(2), cy=Math.max(12,Math.min(166,y(marker.price)+(marker.type==='buy'?17:-17))).toFixed(2);
    return `<g class="chart-marker ${marker.type}"><title>${marker.label}规则触发于${time(marker.time)}，历史信号</title><circle cx="${cx}" cy="${cy}" r="8"/><text x="${cx}" y="${Number(cy)+3}">${marker.type==='buy'?'B':'S'}</text></g>`;
  }).join('');
  $('#chart').innerHTML = `<svg viewBox="0 0 520 180" aria-hidden="true"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${stroke}" stop-opacity="0.18"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>${[35, 90, 145].map(line => `<line x1="8" y1="${line}" x2="448" y2="${line}" class="chart-grid"/><text x="458" y="${line + 4}" class="chart-axis-label">${price(upper - (line - 20) / 140 * (upper - lower), item)}</text>`).join('')}${paths}<circle cx="448" cy="${y(values.at(-1)).toFixed(2)}" r="3.5" fill="${stroke}"/>${markers}</svg>`;
  const mid = bars[Math.floor(bars.length / 2)];
  $('#chart-axis').innerHTML = `<span>${time(bars[0].time)}</span><span>${time(mid.time)}</span><span>${time(bars.at(-1).time)} 上海</span>`;
}

function renderDetail() {
  const item = state.snapshot.instruments.find(item => item.id === state.selected) || state.snapshot.instruments[0];
  if (!item) {
    $('#detail-name').textContent='搜索并添加一个品种'; $('#detail-symbol').textContent='开始建立你的自选';
    $('#detail-price').textContent='—'; $('#detail-change').textContent=''; $('#detail-kind').textContent='';
    $('#selected-action').className='trade-tag hidden'; $('#strategy-label').classList.add('hidden');
    $('#chart').innerHTML='<div class="chart-empty">添加自选后查看图表与买卖信号</div>';
    $('#chart-axis').innerHTML=''; $('#detail-stats').innerHTML=''; $('#strategy-levels').innerHTML='';
    $('#research-records').textContent='';$('#research-status').textContent='请先选择证券。';$('#evaluation-stats').textContent='';$('#evaluation-note').textContent='';$('#position-status').textContent='请先添加并选择证券。';$('#strategy-reason').innerHTML=formatStrategyTips(null,{action:null,reason:''}); $('#strategy-time').textContent=''; $('#market-rule').textContent='';return;
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
  $('#chart-caption').textContent = state.snapshot.mode === 'demo' ? '模拟走势' : `${Math.min(item.hourlyBars?.length||0,60)} 根60分钟收盘 · 按交易时段连续`;
  drawChart(item);
  $('#detail-stats').innerHTML = [['前收盘', price(item.quote?.previousClose, item)], ['成交量 · 源单位', compact(item.quote?.volume)], ['行情时间 · 上海', item.quote?.timeLabel || time(item.quote?.timestamp)], item.quote?.receivedAt ? ['读取时间 · 上海', time(item.quote.receivedAt, true)] : [item.market === 'us' ? '行情时间 · 纽约' : '计价货币', item.market === 'us' ? time(item.quote?.timestamp, false, marketZones.us) : item.currency]].map(([label, value]) => `<div><span>${label}</span><strong>${escape(value)}</strong></div>`).join('');
  $('#strategy-label').textContent = signal.action?.label || '';
  $('#strategy-label').className = signal.action ? signal.action.type : 'hidden';
  $('#strategy-reason').innerHTML = formatStrategyTips(item, signal);
  const p=item.swings?.[state.profile]?.prepared.p||PROFILES[state.profile];
  $('#strategy-levels').innerHTML=signal.action?.type==='buy'&&signal.levels?`<div class="levels">${[['触发价参考',signal.levels.reference],['目标价参考',signal.levels.target],['初始风险观察',Math.max(signal.levels.drawdown,signal.levels.loss)]].map(([label,value])=>`<div><span>${label}</span><strong>${price(value,item)}</strong></div>`).join('')}</div><p class="level-note">目标${p.target*100}% · 峰值回撤线需手填持仓 · 风险位不保证成交</p>`:'';
  $('#strategy-time').textContent=signal.action?.eventKey?`时间口径 · 持仓参考 · 源报价 ${time(signal.action.time,true)} · 非账户同步`:signal.barTime?`时间口径 · 60m收盘 ${time(signal.barTime,true)} · ${p.label}最长${p.days}个交易日`:'时间口径 · 仅已收盘数据';
  renderEvaluation(item);renderPosition(item);renderResearch(item);
  $('#market-rule').textContent = item.rule;
}

function renderSignals() {
  const items = state.snapshot.instruments.filter(item => strategy(item).action);
  $('#signal-grid').innerHTML = items.length ? items.map(item => {
    const signal = strategy(item);
    return `<button class="signal-card" data-select="${escape(item.id)}"><div class="signal-card-top"><span class="trade-tag ${signal.action.type}">${signal.action.label}</span><span class="small-label">${PROFILES[state.profile].label} · 60 MIN</span></div><h3>${escape(item.name)} <span>${escape(item.symbol)}</span></h3>${formatSignalCardBody(signal)}<span class="small-label">${time(signal.action.time)} · ${signal.action.eventKey?'持仓参考':'收盘触发'}${state.snapshot.mode === 'demo' ? ' · 模拟' : ''}</span></button>`;
  }).join('') : '<div class="empty">当前没有同时满足规则的买卖信号。</div>';
}

function render() {
  if (!state.snapshot) return;
  const demo = state.snapshot.mode === 'demo';
  const disconnected = !state.connected;
  const providerDown=!demo&&state.snapshot.status?.connected===false;
  $('#data-banner').classList.toggle('live-banner', !demo && !disconnected && !providerDown && !state.processingIssue);
  const bannerTitle = disconnected ? '暂停 · 本机看板暂未响应'
    : state.processingIssue ? '暂停 · 行情处理失败'
    : providerDown ? '暂停 · Wind连接中断'
    : demo ? '观望 · 模拟体验（非实盘）'
    : '外部行情 · 请核对权限与延迟';
  const bannerWhy = disconnected ? (escape(state.transportIssue)||'保留最近画面；恢复后自动继续，无需刷新。')
    : state.processingIssue ? escape(state.processingIssue)
    : demo ? '价格由程序生成，仅验界面与规则；接入行情后才可跟踪真实市场。'
    : '源时间>90秒未更新则停止该品种策略判断；接入≠交易授权。';
  $('#data-banner').innerHTML = `<span class="banner-icon">${disconnected||providerDown||state.processingIssue ? '!' : demo ? 'D' : '↗'}</span><div class="banner-tip"><strong>${bannerTitle}</strong><span>${bannerWhy}</span></div>`;
  if (!demo && !disconnected && !state.processingIssue && state.snapshot.status?.message) {
    $('#data-banner .banner-tip > span').textContent = state.snapshot.status.message+(state.snapshot.status.lastQuoteReadAt?` 最近读取 ${time(state.snapshot.status.lastQuoteReadAt,true)}；源时间未更新仍暂停对应信号。`:'');
  }
  $('#source-label').textContent = state.snapshot.source;
  $('#directory-status').textContent=state.snapshot.directoryStatus || '输入代码或名称查找证券，点击添加开始读取行情。';
  const p={...PROFILES[state.profile],...riskSettings[state.profile]};
  $('#profile-summary').innerHTML=`<span class="profile-chip">目标 <b>${p.target*100}%</b></span><span class="profile-chip">峰值回撤 ≤<b>${Math.round(p.drawdown*100)}%</b></span><span class="profile-chip">买入亏损 ≤<b>${Math.round(p.loss*100)}%</b></span>`;
  renderOverview(); renderTable(); renderDetail(); renderSignals();renderMacro();
}

function renderMacro(){
  const macro=state.snapshot?.macro,local=state.snapshot?.provider==='wind-local';
  const form=$('#macro-form');for(const node of form.querySelectorAll('input,select,button'))node.disabled=!local;
  if(!macro){$('#macro-status').textContent='自动宏观采集请在已连接Wind的Windows本机版使用。';return;}
  const version=JSON.stringify(macro.config);
  if(form.dataset.version!==version){
    for(const key of ['pmi','dr007','omo7d'])$('#macro-'+key).value=macro.config.codes[key]||'';
    $('#macro-enabled').checked=macro.config.enabled;$('#macro-interval').value=String(macro.config.refreshSeconds);
    form.dataset.version=version;
  }
  $('#macro-status').textContent=(state.connected?'':'本机连接中断，以下保留最近资料。')+macro.message+
    (macro.readAt?` 最近读取：${time(macro.readAt,true)}。`:'');
  $('#macro-values').innerHTML=macro.series.map(s=>`<div class="macro-value"><strong>${escape(s.name)}</strong><span>${s.latest?escape(s.latest.value)+(s.unit==='percent'?'%':''):'—'}</span><small>${escape(s.code||'待配置')} · 观察期 ${escape(s.latest?.observationDate||'—')}${s.error?' · '+escape(s.error):''}</small></div>`).join('');
  const background=createContextPolicy(macro.liveContext).macroAt(now());
  $('#macro-background').textContent='背景规则观察：'+(background.known?background.reason:background.reason+'，等待资料。')+' 尚未参与当前买卖标记。';
  $('#macro-history-note').textContent=macro.historyNote;
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
  if(event.target.closest('#search-retry')){clearTimeout(searchTimer);search($('#search').value.trim(),++searchGeneration,true);return;}
  const add=event.target.closest('[data-add]');
  if(add) {
    add.disabled=true;
    try { const data=await api('/api/watchlist',{code:add.dataset.add}); state.selected=data.instrument.id;save('barry-selected',state.selected);++searchGeneration;clearTimeout(searchTimer);searchController?.abort();$('#search').value='';$('#search-results').classList.add('hidden');await refresh();setMarket('all');toast('已添加自选，正在读取行情。'); } catch(error){toast(error.message);add.disabled=false;} return;
  }
  const remove=event.target.closest('[data-remove]');
  if(remove){try{await api('/api/watchlist/remove',{id:remove.dataset.remove});await refresh();toast('已移除自选。');}catch(error){toast(error.message);}return;}
  const view=event.target.closest('[data-view]');
  if(view){for(const name of ['market','macro','alerts'])$('#'+name+'-view').classList.toggle('hidden',name!==view.dataset.view);document.querySelectorAll('[data-view]').forEach(button=>button.classList.toggle('active',button===view));return;}
  const mode=event.target.closest('[data-profile]');
  if(mode){state.profile=mode.dataset.profile;document.querySelectorAll('[data-profile]').forEach(button=>button.classList.toggle('active',button===mode));render();return;}
  const selection = event.target.closest('[data-select]');
  if (selection) { state.selected = selection.dataset.select; save('barry-selected', state.selected); render(); if(innerWidth<640)document.querySelector('.detail-panel').scrollIntoView({behavior:'smooth',block:'start'});return; }
  const market = event.target.closest('[data-market], [data-overview]');
  if (market) setMarket(market.dataset.market || market.dataset.overview);
});
let searchTimer,searchController,searchGeneration=0;
async function search(query,generation,retry=false){
  if(!query){$('#search-results').classList.add('hidden');return;}
  searchController?.abort();searchController=new AbortController();
  try{
    const response=await fetch(`/api/search?q=${encodeURIComponent(query)}${retry?'&retry=1':''}`,{signal:searchController.signal});
    if(!response.ok)throw Error('搜索服务暂不可用');
    const data=await response.json();if(generation!==searchGeneration)return;
    $('#search-results').classList.remove('hidden');
    $('#search-results').innerHTML=(data.results||[]).map(item=>`<div class="search-result"><div><strong>${escape(item.name)}</strong><span>${escape(item.symbol)} · ${escape(item.kind)} · ${item.currency}</span><span>${escape(item.nameSource||'证券资料')}${item.preview?` · 查询返回价 ${price(item.preview.price,item)}（行情时间待确认）`:item.namePending?' · 名称/行情待核实':''}</span></div><button class="button ${item.added?'secondary':''}" data-add="${escape(item.id)}" ${item.added?'disabled':''}>${item.added?'已添加':item.namePending?'+ 按代码添加':'+ 添加'}</button></div>`).join('')+`<p class="search-note">${escape(data.pending?'Wind 正在校验代码，请稍候……':data.message||'输入六位股票或ETF代码即可搜索，无需填写交易所。')}</p>${data.canRetry?'<button class="button secondary" id="search-retry">重新查询</button>':''}`;
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
$('#macro-form').addEventListener('submit',async event=>{
  event.preventDefault();
  try{
    if(state.snapshot?.provider!=='wind-local')throw Error('请在Wind本机版设置宏观采集。');
    await api('/api/macro/config',{enabled:$('#macro-enabled').checked,refreshSeconds:Number($('#macro-interval').value),
      codes:Object.fromEntries(['pmi','dr007','omo7d'].map(key=>[key,$('#macro-'+key).value]))});
    await refresh();toast('宏观设置已保存。');
  }catch(error){toast(error.message);}
});
$('#macro-refresh').addEventListener('click',async()=>{
  try{await api('/api/macro/refresh',{});toast('已请求读取；请保持Wind和本机窗口运行。');await refresh();}catch(error){toast(error.message);}
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
  $('#connection-text').textContent = connected ? '网页已连接 · 自动更新' : '看板暂未响应 · 自动重试';
}
async function api(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败，请重试。');return data;
}
function acceptSnapshot(snapshot,connected=false){
  state.snapshot=snapshot;
  computeSwings(snapshot);
  state.processingIssue='';state.transportIssue='';
  if(!snapshot.instruments.some(item=>item.id===state.selected))state.selected=snapshot.instruments[0]?.id||'';
  state.receivedAt=performance.now();
  if(connected)setConnection(true);
  render();scanAlerts();
}
async function refresh(){const frame=await connection.refresh();if(!frame)throw Error('本机看板暂未响应，正在自动重试。');}
function renderResearch(item) {
  const research=item.research;
  $('#research-status').textContent=research?.message||'补充资料仅在Wind本机版接入；当前未连接Capital IQ或Bloomberg。';
  $('#research-bloomberg').disabled=Boolean(research?.busy);
  const labels={PE_RATIO:'市盈率',PX_TO_BOOK_RATIO:'市净率',EQY_DVD_YLD_IND:'预期股息率',CRNCY:'币种'};
  $('#research-records').innerHTML=(research?.records||[]).map(row=>`<p><strong>${escape(row.source)} · ${escape(labels[row.field]||row.field)}</strong>：${escape(row.value)} ${escape(row.unit||'')}<br><span>${escape(row.method||'')} · 数据时点 ${escape(row.as_of||'未提供')} · 读取/导入 ${escape(row.read_at||'未提供')}</span></p>`).join('')||'<p>暂无该证券资料。CIQ网页账号不等于API授权，可先导出后导入。</p>';
}
$('#research-bloomberg').addEventListener('click',async()=>{
  try{if(state.snapshot?.provider!=='wind-local')throw Error('请在Windows Wind本机版使用。');await api('/api/research/bloomberg',{id:state.selected});await refresh();toast('已提交本机Bloomberg查询。');}catch(error){toast(error.message);}
});
$('#research-import').addEventListener('click',()=>$('#research-file').click());
$('#research-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{if(state.snapshot?.provider!=='wind-local')throw Error('资料导入请在Windows本机版使用。');if(file.size>1500000)throw Error('CSV请小于1.5MB。');const bytes=await file.arrayBuffer();let text=new TextDecoder('utf-8').decode(bytes);if(text.includes('\ufffd'))text=new TextDecoder('gb18030').decode(bytes);const result=await api('/api/research/import',{csv:text,source:$('#research-source').value});await refresh();toast(`已导入${result.imported}项资料。`);}catch(error){toast(error.message);}finally{event.target.value='';}
});
function renderEvaluation(item) {
  const data=item.swings?.[state.profile],stats=data?.prepared.evaluation;
  $('#evaluation-mode').textContent=PROFILES[state.profile].label+(state.snapshot.mode==='demo'?' · 模拟样本':'');
  const eligible=item.market!=='futures'&&(item.dailyBars||[]).length&&(!item.swingIssue);
  const format=v=>v===null?'暂无样本':(v*100).toFixed(1)+'%';
  $('#evaluation-stats').innerHTML=eligible&&stats?`<div class="detail-stats">${[['综合成功率',format(stats.successRate)],['目标达成率',format(stats.targetRate)],['风险合格率',format(stats.riskRate)],['已完成 / 观察中',`${stats.completed} / ${stats.pending}`]].map(([a,b])=>`<div><span>${a}</span><strong>${b}</strong></div>`).join('')}</div>`:'尚无可评估历史。';
  const hours=item.hourlyBars||[];
  $('#evaluation-note').innerHTML=eligible?tipStack([
    tipRow('样本窗口', `${hours[0]?.date||''} → ${hours.at(-1)?.date||''} · ${state.snapshot.mode==='demo'?'模拟':'Wind'}`),
    tipRow('成交口径', '不重叠多头 · 下一根开盘买 · A股T+1 · 不计手续费'),
    tipRow('统计注意', `同根顺序不明/缺失 ${stats?.ambiguous||0} 笔不计成功${(stats?.completed||0)<30?' · 样本<30不足':''} · 未独立验证，不代表未来准确率`, 'tip-mute'),
  ]):tipStack([tipRow('当前结论','等待评估','tip-mute'), tipRow('还差什么','真实日线、60分钟历史与时间口径')]);
  if(!['risk-drawdown','risk-loss'].includes(document.activeElement.id)){
    const p=data?.prepared.p||PROFILES[state.profile];$('#risk-drawdown').value=Math.round(p.drawdown*100);$('#risk-loss').value=Math.round(p.loss*100);
  }
}
function renderPosition(item) {
  const key=`${item.id}:${state.profile}`,position=positions[key],holding=item.swings?.[state.profile]?.holding;
  $('#position-profile').textContent=PROFILES[state.profile].label;
  if(!$('#position-form').contains(document.activeElement)){
    $('#position-entry').value=position?.entry||'';$('#position-date').value=position?.date||'';$('#position-peak').value=position?.peak||'';
  }
  $('#position-status').textContent=holding?.gain!==undefined?`当前盈亏 ${pct(holding.gain*100)} · 已持有${holding.heldDays}个交易日 · 当前峰值回撤 ${(holding.drawdown*100).toFixed(2)}% · 已记录最大回撤 ${((holding.position.maxDrawdown||0)*100).toFixed(2)}% · 已记录最大亏损 ${((holding.position.maxLoss||0)*100).toFixed(2)}%${holding.locked?' · A股当日按T+1锁定':''}。最高价依赖手填及页面实际观察，不保证覆盖离线期间。`:position?'已保存持仓；等待有效行情。':'未填写买入价，无法判断你的实际盈亏。';
}
$('#risk-save').addEventListener('click',()=>{
  const drawdown=Number($('#risk-drawdown').value)/100,loss=Number($('#risk-loss').value)/100;
  if(![drawdown,loss].every(v=>v>=.01&&v<=.5)){toast('风险上限请填写1%到50%。');return;}
  riskSettings[state.profile]={drawdown,loss};save('barry-swing-risk',riskSettings);computeSwings(state.snapshot);render();scanAlerts();toast('风险上限已保存，历史评估已重新计算。');
});
$('#position-form').addEventListener('submit',event=>{
  event.preventDefault();const entry=Number($('#position-entry').value),peak=Number($('#position-peak').value),date=$('#position-date').value;
  const item=state.snapshot.instruments.find(item=>item.id===state.selected);
  if(!item||!Number.isFinite(entry)||entry<=0||!Number.isFinite(peak)||peak<entry||!date||date>dayInZone(Date.now(),marketZones[item.market])){toast('请核对买入价、日期；最高价不能低于买入价。');return;}
  positions[`${item.id}:${state.profile}`]={entry,peak,date};save('barry-swing-positions',positions);computeSwings(state.snapshot);document.activeElement.blur();render();scanAlerts();toast('持仓参考已保存。');
});
$('#position-clear').addEventListener('click',()=>{delete positions[`${state.selected}:${state.profile}`];save('barry-swing-positions',positions);computeSwings(state.snapshot);document.activeElement.blur();render();});
const alertPrefs={enabled:false,short:true,medium:true,desktop:false,sound:false,...readStored('barry-alert-prefs',{})};
let audioContext;
const history=readStored('barry-alert-history',[]);
function alertStatus(text){$('#alert-status').textContent=text;$('#alert-indicator').textContent=alertPrefs.enabled?'开启':'关闭';}
function renderAlertHistory(){$('#alert-history').innerHTML=history.length?history.slice(0,30).map(event=>`<p><strong>${escape(event.title)}</strong> · ${time(event.at,true)}<br>${escape(event.body)}</p>`).join(''):'暂无新提醒。';}
function emitAlert(event){
  history.unshift(event);history.splice(100);save('barry-alert-history',history);renderAlertHistory();toast(event.title+'：'+event.body);
  if(alertPrefs.sound&&audioContext){audioContext.resume().then(()=>playChime(audioContext)).catch(()=>alertStatus('声音未能播放，请点击测试按钮。'));}
  if(alertPrefs.desktop&&'Notification'in window&&Notification.permission==='granted'){
    try{const notification=new Notification(event.title,{body:event.body,tag:event.key});setTimeout(()=>notification.close(),15000);}catch{alertStatus('桌面弹窗不可用，请检查浏览器和Windows通知设置。');}
  }
}
const alertEngine=new AlertEngine({seen:readStored('barry-alert-seen',[]),emit:emitAlert,save:seen=>save('barry-alert-seen',seen)});
function scanAlerts(){
  if(!state.snapshot)return;const events=[];
  for(const item of state.snapshot.instruments)for(const mode of ['short','medium']){
    const signal=strategy(item,mode).action;if(!signal||!alertPrefs[mode])continue;
    events.push({key:`${item.id}:${mode}:${signal.type}:${signal.eventKey||signal.time}`,title:`${state.snapshot.mode==='demo'?'[模拟] ':''}${item.name} · ${PROFILES[mode].label}${signal.label}`,body:signal.reason,at:Date.now()});
  }
  alertEngine.scan(events,alertPrefs.enabled&&state.connected);
}
for(const [id,key] of [['alerts-enabled','enabled'],['alerts-short','short'],['alerts-medium','medium'],['alerts-desktop','desktop'],['alerts-sound','sound']]){
  $('#'+id).checked=alertPrefs[key];
  $('#'+id).addEventListener('change',async event=>{
    alertPrefs[key]=event.target.checked;
    if(key==='short'||key==='medium')alertEngine.initial=true;
    if(key==='desktop'&&alertPrefs.desktop){
      if(!('Notification'in window)){alertPrefs.desktop=false;alertStatus('当前浏览器不支持桌面通知。');}
      else {try{const permission=await Notification.requestPermission();if(permission!=='granted'){alertPrefs.desktop=false;alertStatus('桌面通知未获允许，请在浏览器网站设置中开启。');}}catch{alertPrefs.desktop=false;alertStatus('浏览器未能申请通知权限。');}}
      event.target.checked=alertPrefs.desktop;
    }
    if((key==='sound'||key==='enabled')&&alertPrefs.sound&&alertPrefs.enabled){try{audioContext||=new(window.AudioContext||window.webkitAudioContext)();await audioContext.resume();playChime(audioContext);}catch{alertPrefs.sound=false;$('#alerts-sound').checked=false;alertStatus('声音初始化失败，请检查浏览器设置。');}}
    save('barry-alert-prefs',alertPrefs);alertStatus(`提醒${alertPrefs.enabled?'开启':'关闭'}；桌面${alertPrefs.desktop?'开启':'关闭'}；声音${alertPrefs.sound?'开启':'关闭'}。${'Notification'in window&&Notification.permission==='denied'?'桌面通知被浏览器拒绝，请在网站设置中允许。':''}`);scanAlerts();
  });
}
$('#alert-test').addEventListener('click',async()=>{
  if(alertPrefs.sound){audioContext||=new(window.AudioContext||window.webkitAudioContext)();await audioContext.resume();playChime(audioContext);}
  if(alertPrefs.desktop&&'Notification'in window&&Notification.permission==='granted'){const n=new Notification('Barry 提醒测试',{body:'这是测试通知，不是交易信号。'});setTimeout(()=>n.close(),5000);}
  alertStatus(`测试已执行。提醒总开关${alertPrefs.enabled?'开启':'关闭'}；声音${alertPrefs.sound?'开启':'关闭'}；桌面${alertPrefs.desktop?'开启':'关闭'}。`);
});
$('#clear-alerts').addEventListener('click',()=>{history.splice(0);save('barry-alert-history',history);renderAlertHistory();});
alertStatus(alertPrefs.enabled?`已开启；只提醒之后的新信号。${alertPrefs.sound?'重新打开网页后，请点测试按钮激活声音。':''}`:'提醒关闭；可分别启用声音和桌面弹窗。');renderAlertHistory();
const connection=new MarketConnection({
  onSnapshot:frame=>acceptSnapshot(frame,true),
  onConnection:connected=>{const changed=state.connected!==connected;setConnection(connected);if(changed){try{render();}catch{}}},
  onError:(message,detail)=>{if(detail?.processing)state.processingIssue=message;else state.transportIssue=message;try{render();}catch{$('#connection-text').textContent=message;}},
});
connection.start();
window.addEventListener('pagehide',()=>connection.stop());
window.addEventListener('pageshow',()=>connection.start());
let freshnessKey = '';
setInterval(() => {
  $('#clock').textContent = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(Date.now());
  if (state.snapshot) {
    const key = state.snapshot.instruments.map(item => fresh(item) ? '1' : '0').join('');
    if (key !== freshnessKey) { freshnessKey = key; render(); }
  }
}, 1000);
