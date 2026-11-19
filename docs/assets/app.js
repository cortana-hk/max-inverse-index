'use strict';

const DATA_ROOT = 'https://raw.githubusercontent.com/cortana-hk/max-inverse-index/main/data';
const REFRESH_MS = 60_000;
const STALE_MS = 3 * 60_000;
const ACTION_LABELS = { buy: '买入', sell: '卖出', new: '新建', increase: '增持', reduce: '减持', close: '清仓' };
let hasSuccessfulLoad = false;
let cachedData = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function signed(value, suffix = '%') {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'N/A';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}${suffix}`;
}

function formatPrice(value, currency) {
  if (!Number.isFinite(Number(value))) return 'N/A';
  const prefix = currency === 'HKD' ? 'HK$' : '$';
  return `${prefix}${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

function applyTone(node, value) {
  node.classList.remove('positive', 'negative', 'neutral');
  node.classList.add(value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral');
}

function formatTime(value, includeDate = true) {
  if (!value) return '等待首个快照';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-HK', {
    timeZone: 'Asia/Hong_Kong', month: includeDate ? '2-digit' : undefined,
    day: includeDate ? '2-digit' : undefined, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function setStatus(state, label) {
  document.getElementById('live-pill').dataset.state = state;
  document.getElementById('live-label').textContent = label;
}

function basePoint() {
  const value = Number(document.getElementById('base-point').value);
  return Number.isFinite(value) && value > 0 ? value : 100;
}

function scaledIndex(value) {
  return Number(value) * basePoint() / 100;
}

function renderMetrics(latest) {
  const total = document.getElementById('total-return');
  const day = document.getElementById('day-return');
  total.textContent = signed(latest.total_return_pct);
  day.textContent = signed(latest.day_return_pct);
  applyTone(total, Number(latest.total_return_pct));
  applyTone(day, Number(latest.day_return_pct));
  document.getElementById('mii-index').textContent = scaledIndex(latest.mii_index).toFixed(2);
}

function renderHoldings(holdings) {
  const list = document.getElementById('holdings-list');
  list.replaceChildren();
  document.getElementById('holding-count').textContent = `${holdings.length} 个标的`;
  if (!holdings.length) {
    list.append(element('p', 'placeholder', '当前空仓。'));
    return;
  }
  holdings.forEach((holding) => {
    const row = element('div', 'holding-row');
    const identity = element('span');
    identity.append(element('span', 'symbol', holding.symbol));
    identity.append(element('span', 'market', holding.market));

    const weight = element('span', 'weight');
    weight.append(element('span', '', signed(holding.weight_pct)));
    const track = element('span', 'weight-track');
    const fill = element('span', 'weight-fill');
    fill.style.width = `${Math.min(Math.abs(Number(holding.weight_pct)), 100)}%`;
    track.append(fill);
    weight.append(track);

    const pnl = element('span', 'pnl', signed(holding.unrealized_pnl_pct));
    applyTone(pnl, Number(holding.unrealized_pnl_pct));
    row.append(
      identity, weight,
      element('span', 'price', formatPrice(holding.average_cost, holding.currency)),
      element('span', 'price', formatPrice(holding.current_price, holding.currency)),
      pnl,
    );
    list.append(row);
  });
}

function renderEvents(events) {
  const list = document.getElementById('events-list');
  list.replaceChildren();
  document.getElementById('trade-count').textContent = `${events.length} 笔订单`;
  if (!events.length) {
    list.append(element('p', 'placeholder', '暂无成交记录。'));
    return;
  }
  [...events].reverse().forEach((event) => {
    const action = ACTION_LABELS[event.action] || '变化';
    const row = element('div', 'trade-row');
    row.dataset.action = event.action;
    const detail = event.price !== undefined
      ? formatPrice(event.price, event.currency)
      : `${Number(event.previous_weight_pct).toFixed(2)}% → ${Number(event.weight_pct).toFixed(2)}%`;
    row.append(
      element('time', 'trade-time', formatTime(event.time)),
      element('span', 'symbol', event.symbol),
      element('span', 'trade-action', action),
      element('span', 'price', detail),
    );
    list.append(row);
  });
}

function renderChart(history) {
  const svg = document.getElementById('performance-chart');
  const empty = document.getElementById('chart-empty');
  const axis = document.getElementById('chart-axis');
  svg.replaceChildren();
  axis.replaceChildren();
  document.getElementById('point-count').textContent = `${history.length} 个数据点`;
  if (!history.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const points = history.filter((point) => Number.isFinite(Number(point.mii_index)));
  if (!points.length) return;
  const values = points.map((point) => scaledIndex(point.mii_index));
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * 0.18, basePoint() * 0.01);
  minimum -= padding;
  maximum += padding;
  const x = (index) => points.length === 1 ? 500 : index / (points.length - 1) * 1000;
  const y = (value) => 204 - (value - minimum) / (maximum - minimum) * 188;
  const namespace = 'http://www.w3.org/2000/svg';
  const defs = document.createElementNS(namespace, 'defs');
  const gradient = document.createElementNS(namespace, 'linearGradient');
  gradient.setAttribute('id', 'area-gradient');
  gradient.setAttribute('x1', '0');
  gradient.setAttribute('y1', '0');
  gradient.setAttribute('x2', '0');
  gradient.setAttribute('y2', '1');
  [['0%', '0.18'], ['100%', '0']].forEach(([offset, opacity]) => {
    const stop = document.createElementNS(namespace, 'stop');
    stop.setAttribute('offset', offset);
    stop.setAttribute('stop-color', '#c9f66f');
    stop.setAttribute('stop-opacity', opacity);
    gradient.append(stop);
  });
  defs.append(gradient);
  svg.append(defs);
  [16, 63, 110, 157, 204].forEach((lineY) => {
    const line = document.createElementNS(namespace, 'line');
    line.setAttribute('x1', '0'); line.setAttribute('x2', '1000');
    line.setAttribute('y1', String(lineY)); line.setAttribute('y2', String(lineY));
    line.setAttribute('class', 'chart-grid'); svg.append(line);
  });
  const coordinates = points.map((point, index) => `${x(index)},${y(scaledIndex(point.mii_index))}`);
  if (points.length > 1) {
    const area = document.createElementNS(namespace, 'polygon');
    area.setAttribute('points', `0,220 ${coordinates.join(' ')} 1000,220`);
    area.setAttribute('class', 'chart-area'); svg.append(area);
  }
  const line = document.createElementNS(namespace, 'polyline');
  line.setAttribute('points', coordinates.join(' ')); line.setAttribute('class', 'chart-line'); svg.append(line);
  const dot = document.createElementNS(namespace, 'circle');
  dot.setAttribute('cx', String(x(points.length - 1)));
  dot.setAttribute('cy', String(y(scaledIndex(points[points.length - 1].mii_index))));
  dot.setAttribute('r', '4'); dot.setAttribute('class', 'chart-point'); svg.append(dot);
  [points[0].time, points[points.length - 1].time].forEach((time) => axis.append(element('span', '', formatTime(time))));
}

function updateFreshness(latest) {
  if (!latest.generated_at) { setStatus('loading', '等待首个快照'); return; }
  const timestamp = new Date(latest.generated_at).getTime();
  if (!Number.isFinite(timestamp)) { setStatus('loading', '等待首个快照'); return; }
  setStatus(Date.now() - timestamp > STALE_MS ? 'stale' : 'live', Date.now() - timestamp > STALE_MS ? '数据可能过期' : '近实时 · 自动刷新');
}

function validatePayload(latest, history, events) {
  if (!latest || latest.schema_version !== 1 || !Array.isArray(latest.holdings)) throw new Error('Invalid latest snapshot');
  if (!Array.isArray(history) || !Array.isArray(events)) throw new Error('Invalid history');
}

function renderAll() {
  if (!cachedData) return;
  renderMetrics(cachedData.latest);
  renderHoldings(cachedData.latest.holdings);
  renderEvents(cachedData.events);
  renderChart(cachedData.history);
}

async function fetchJson(name) {
  const response = await fetch(`${DATA_ROOT}/${name}.json?t=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  try {
    const [latest, history, events] = await Promise.all([fetchJson('latest'), fetchJson('history'), fetchJson('events')]);
    validatePayload(latest, history, events);
    cachedData = { latest, history, events };
    renderAll();
    document.getElementById('updated-at').textContent = `数据时间 ${formatTime(latest.generated_at)} HKT · 每 60 秒检查更新`;
    updateFreshness(latest);
    hasSuccessfulLoad = true;
    document.getElementById('updated-at').dataset.timestamp = latest.generated_at;
  } catch (error) {
    console.warn('MII data refresh failed:', error.message);
    setStatus('error', hasSuccessfulLoad ? '刷新失败 · 展示上次数据' : '暂时无法读取数据');
    if (!hasSuccessfulLoad) document.getElementById('updated-at').textContent = '公开数据尚未就绪，请稍后刷新';
  }
}

try {
  const savedBase = Number(localStorage.getItem('mii-base-point'));
  if (Number.isFinite(savedBase) && savedBase > 0) document.getElementById('base-point').value = savedBase;
} catch (error) { /* localStorage may be disabled */ }
document.getElementById('base-point').addEventListener('input', () => {
  const value = basePoint();
  try { localStorage.setItem('mii-base-point', String(value)); } catch (error) { /* localStorage may be disabled */ }
  renderMetrics(cachedData ? cachedData.latest : { total_return_pct: null, day_return_pct: null, mii_index: 100 });
  if (cachedData) renderChart(cachedData.history);
});

refresh();
setInterval(refresh, REFRESH_MS);
setInterval(() => {
  const timestamp = document.getElementById('updated-at').dataset.timestamp;
  if (hasSuccessfulLoad && timestamp) updateFreshness({ generated_at: timestamp });
}, 30_000);
