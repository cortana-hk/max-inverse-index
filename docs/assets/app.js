'use strict';

const DATA_ROOT = 'https://raw.githubusercontent.com/cortana-hk/max-inverse-index/main/data';
const REFRESH_MS = 60_000;
const STALE_MS = 3 * 60_000;
const DISPLAY_ZONE = 'America/New_York';
const ACTION_LABELS = { buy: '买入', sell: '卖出', new: '新建', increase: '增持', reduce: '减持', close: '清仓' };
let hasSuccessfulLoad = false;
let cachedData = null;
let currentInstance = 'max_data';
let currentInstanceStartDate = null;
let instanceRegistry = null;
let loadGeneration = 0;

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
  return new Intl.DateTimeFormat('zh-US', {
    timeZone: DISPLAY_ZONE, month: includeDate ? '2-digit' : undefined,
    day: includeDate ? '2-digit' : undefined, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function dateInDisplayZone(value) {
  const date = new Date(value);
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: DISPLAY_ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function setStatus(state, label) {
  document.getElementById('live-pill').dataset.state = state;
  document.getElementById('live-label').textContent = label;
}

function validHistory(history) {
  return history.filter((point) => Number.isFinite(Number(point.mii_index)) && Number(point.mii_index) > 0);
}

function baselinePoint(points) {
  if (!points.length) return null;
  const selected = currentInstanceStartDate || dateInDisplayZone(points[0].time);
  return points.find((point) => dateInDisplayZone(point.time) >= selected) || points[points.length - 1];
}

function rebasedIndex(value, baseline) {
  return baseline ? Number(value) / Number(baseline.mii_index) * 100 : 100;
}

function renderMetrics(latest, baseline) {
  const index = rebasedIndex(latest.mii_index, baseline);
  const totalChange = baseline ? index - 100 : null;
  const total = document.getElementById('total-return');
  const day = document.getElementById('day-return');
  total.textContent = signed(totalChange);
  day.textContent = signed(latest.day_return_pct);
  applyTone(total, Number(totalChange));
  applyTone(day, Number(latest.day_return_pct));
  document.getElementById('mii-index').textContent = index.toFixed(2);
  document.getElementById('baseline-caption').textContent = baseline
    ? `从 ${dateInDisplayZone(baseline.time)} 至今`
    : '从最早数据日至今';
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

    const pricePair = element('span', 'price-pair');
    pricePair.append(element('span', 'cost-price', formatPrice(holding.average_cost, holding.currency)));
    const direction = Number(holding.current_price) > Number(holding.average_cost) ? 'up' : Number(holding.current_price) < Number(holding.average_cost) ? 'down' : 'flat';
    pricePair.append(element('span', `price-arrow ${direction}`, direction === 'up' ? '↗' : direction === 'down' ? '↘' : '→'));
    pricePair.append(element('span', `current-price ${direction}`, formatPrice(holding.current_price, holding.currency)));
    const pnl = element('span', 'pnl', signed(holding.unrealized_pnl_pct));
    applyTone(pnl, Number(holding.unrealized_pnl_pct));
    row.append(identity, weight, pricePair, pnl);
    list.append(row);
  });
}

function renderEvents(events, baseline) {
  const visibleEvents = baseline
    ? events.filter((event) => new Date(event.time) >= new Date(baseline.time))
    : events;
  const list = document.getElementById('events-list');
  list.replaceChildren();
  document.getElementById('trade-count').textContent = `${visibleEvents.length} 笔订单`;
  if (!visibleEvents.length) {
    list.append(element('p', 'placeholder', '暂无成交记录。'));
    return;
  }
  [...visibleEvents].reverse().forEach((event, index) => {
    const action = ACTION_LABELS[event.action] || '变化';
    const row = element('article', `trade-row${index < 3 ? ' recent' : ''}`);
    row.dataset.action = event.action;
    const time = element('time', 'trade-time', formatTime(event.time));
    const rail = element('span', 'trade-rail');
    const body = element('div', 'trade-body');
    const title = element('div', 'trade-title');
    title.append(element('span', 'symbol', event.symbol));
    title.append(element('span', 'trade-action', action));
    if (index === 0) title.append(element('span', 'latest-badge', '最新'));
    const detail = event.price !== undefined
      ? formatPrice(event.price, event.currency)
      : `${Number(event.previous_weight_pct).toFixed(2)}% → ${Number(event.weight_pct).toFixed(2)}%`;
    body.append(title, element('span', 'trade-change', event.price !== undefined ? '成交' : '仓位变化'));
    row.append(time, rail, body, element('span', 'trade-price', detail));
    list.append(row);
  });
}

function renderChart(history, baseline) {
  const svg = document.getElementById('performance-chart');
  const empty = document.getElementById('chart-empty');
  const axis = document.getElementById('chart-axis');
  svg.replaceChildren();
  axis.replaceChildren();
  const allPoints = validHistory(history);
  const points = baseline ? allPoints.filter((point) => new Date(point.time) >= new Date(baseline.time)) : [];
  document.getElementById('point-count').textContent = `${points.length} 个数据点`;
  if (!points.length) { empty.hidden = false; return; }
  empty.hidden = true;
  const values = points.map((point) => rebasedIndex(point.mii_index, baseline));
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * 0.18, 1);
  minimum -= padding;
  maximum += padding;
  const x = (index) => points.length === 1 ? 500 : index / (points.length - 1) * 1000;
  const y = (value) => 204 - (value - minimum) / (maximum - minimum) * 188;
  const namespace = 'http://www.w3.org/2000/svg';
  const defs = document.createElementNS(namespace, 'defs');
  const gradient = document.createElementNS(namespace, 'linearGradient');
  gradient.setAttribute('id', 'area-gradient');
  gradient.setAttribute('x1', '0'); gradient.setAttribute('y1', '0');
  gradient.setAttribute('x2', '0'); gradient.setAttribute('y2', '1');
  [['0%', '0.18'], ['100%', '0']].forEach(([offset, opacity]) => {
    const stop = document.createElementNS(namespace, 'stop');
    stop.setAttribute('offset', offset); stop.setAttribute('stop-color', '#c9f66f'); stop.setAttribute('stop-opacity', opacity);
    gradient.append(stop);
  });
  defs.append(gradient); svg.append(defs);
  [16, 63, 110, 157, 204].forEach((lineY) => {
    const line = document.createElementNS(namespace, 'line');
    line.setAttribute('x1', '0'); line.setAttribute('x2', '1000');
    line.setAttribute('y1', String(lineY)); line.setAttribute('y2', String(lineY));
    line.setAttribute('class', 'chart-grid'); svg.append(line);
  });
  const coordinates = points.map((point, index) => `${x(index)},${y(rebasedIndex(point.mii_index, baseline))}`);
  if (points.length > 1) {
    const area = document.createElementNS(namespace, 'polygon');
    area.setAttribute('points', `0,220 ${coordinates.join(' ')} 1000,220`);
    area.setAttribute('class', 'chart-area'); svg.append(area);
  }
  const line = document.createElementNS(namespace, 'polyline');
  line.setAttribute('points', coordinates.join(' ')); line.setAttribute('class', 'chart-line'); svg.append(line);
  const dot = document.createElementNS(namespace, 'circle');
  dot.setAttribute('cx', String(x(points.length - 1)));
  dot.setAttribute('cy', String(y(rebasedIndex(points[points.length - 1].mii_index, baseline))));
  dot.setAttribute('r', '4'); dot.setAttribute('class', 'chart-point'); svg.append(dot);
  [points[0].time, points[points.length - 1].time].forEach((time) => axis.append(element('span', '', formatTime(time))));
}

function updateFreshness(latest) {
  if (!latest.generated_at) { setStatus('loading', '等待首个快照'); return; }
  const timestamp = new Date(latest.generated_at).getTime();
  if (!Number.isFinite(timestamp)) { setStatus('loading', '等待首个快照'); return; }
  const stale = Date.now() - timestamp > STALE_MS;
  setStatus(stale ? 'stale' : 'live', stale ? '数据可能过期' : '近实时 · 自动刷新');
}

function validatePayload(latest, history, events) {
  if (!latest || latest.schema_version !== 1 || !Array.isArray(latest.holdings)) throw new Error('Invalid latest snapshot');
  if (!Array.isArray(history) || !Array.isArray(events)) throw new Error('Invalid history');
}

function renderAll() {
  if (!cachedData) return;
  const points = validHistory(cachedData.history);
  const baseline = baselinePoint(points);
  renderMetrics(cachedData.latest, baseline);
  renderHoldings(cachedData.latest.holdings);
  renderEvents(cachedData.events, baseline);
  renderChart(cachedData.history, baseline);
}

function applyInstanceStartDate() {
  const instance = instanceRegistry.instances.find((item) => item.id === currentInstance);
  currentInstanceStartDate = instance && instance.start_date ? instance.start_date : null;
}

async function fetchJson(name, instanceId = currentInstance) {
  const response = await fetch(`${DATA_ROOT}/instances/${instanceId}/${name}.json?t=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

async function fetchRegistry() {
  try {
    const response = await fetch(`${DATA_ROOT}/instances.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error('registry unavailable');
    const registry = await response.json();
    if (!Array.isArray(registry.instances) || !registry.instances.length) throw new Error('invalid registry');
    return registry;
  } catch (error) {
    return { default_instance: 'test_data', instances: [{ id: 'test_data', name: 'test_data' }] };
  }
}

function preferredInstance(registry) {
  const available = new Set(registry.instances.map((instance) => instance.id));
  const fromUrl = new URLSearchParams(window.location.search).get('instance');
  if (available.has(fromUrl)) return fromUrl;
  try {
    const saved = localStorage.getItem('mii-instance');
    if (available.has(saved)) return saved;
  } catch (error) { /* storage is optional */ }
  return available.has(registry.default_instance) ? registry.default_instance : registry.instances[0].id;
}

async function refresh() {
  const generation = loadGeneration;
  const instanceId = currentInstance;
  try {
    const [latest, history, events] = await Promise.all([
      fetchJson('latest', instanceId), fetchJson('history', instanceId), fetchJson('events', instanceId),
    ]);
    if (generation !== loadGeneration) return;
    validatePayload(latest, history, events);
    cachedData = { latest, history, events };
    renderAll();
    document.getElementById('updated-at').textContent = `数据时间 ${formatTime(latest.generated_at)} ET · 每 60 秒检查更新`;
    updateFreshness(latest);
    hasSuccessfulLoad = true;
    document.getElementById('updated-at').dataset.timestamp = latest.generated_at;
  } catch (error) {
    if (generation !== loadGeneration) return;
    console.warn('MII data refresh failed:', error.message);
    setStatus('error', hasSuccessfulLoad ? '刷新失败 · 展示上次数据' : '暂时无法读取数据');
    if (!hasSuccessfulLoad) document.getElementById('updated-at').textContent = '公开数据尚未就绪，请稍后刷新';
  }
}

function switchInstance(instanceId) {
  currentInstance = instanceId;
  loadGeneration += 1;
  cachedData = null;
  hasSuccessfulLoad = false;
  document.getElementById('updated-at').dataset.timestamp = '';
  document.getElementById('updated-at').textContent = '正在切换实例…';
  setStatus('loading', '读取实例数据');
  applyInstanceStartDate();
  try { localStorage.setItem('mii-instance', instanceId); } catch (error) { /* storage is optional */ }
  const url = new URL(window.location.href);
  url.searchParams.set('instance', instanceId);
  window.history.replaceState({}, '', url);
  refresh();
}

document.getElementById('instance-select').addEventListener('change', (event) => switchInstance(event.target.value));

async function initialise() {
  const registry = await fetchRegistry();
  instanceRegistry = registry;
  const select = document.getElementById('instance-select');
  select.replaceChildren();
  registry.instances.forEach((instance) => {
    const option = element('option', '', instance.name);
    option.value = instance.id;
    select.append(option);
  });
  currentInstance = preferredInstance(registry);
  select.value = currentInstance;
  applyInstanceStartDate();
  refresh();
}

initialise();
setInterval(refresh, REFRESH_MS);
setInterval(() => {
  const timestamp = document.getElementById('updated-at').dataset.timestamp;
  if (hasSuccessfulLoad && timestamp) updateFreshness({ generated_at: timestamp });
}, 30_000);
