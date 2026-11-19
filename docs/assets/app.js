'use strict';

const DATA_ROOT = 'https://raw.githubusercontent.com/cortana-hk/max-inverse-index/main/data';
const REFRESH_MS = 60_000;
const STALE_MS = 3 * 60_000;
const ACTION_LABELS = { new: '新建', increase: '增持', reduce: '减持', close: '清仓' };
let hasSuccessfulLoad = false;

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

function applyTone(node, value) {
  node.classList.remove('positive', 'negative', 'neutral');
  node.classList.add(value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral');
}

function formatTime(value, includeDate = true) {
  if (!value) return '等待首个快照';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-HK', {
    timeZone: 'Asia/Hong_Kong',
    month: includeDate ? '2-digit' : undefined,
    day: includeDate ? '2-digit' : undefined,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function setStatus(state, label) {
  document.getElementById('live-pill').dataset.state = state;
  document.getElementById('live-label').textContent = label;
}

function renderMetrics(latest) {
  const total = document.getElementById('total-return');
  const day = document.getElementById('day-return');
  total.textContent = signed(latest.total_return_pct);
  day.textContent = signed(latest.day_return_pct);
  applyTone(total, Number(latest.total_return_pct));
  applyTone(day, Number(latest.day_return_pct));
  document.getElementById('mii-index').textContent = Number(latest.mii_index).toFixed(2);
}

function renderHoldings(holdings) {
  const list = document.getElementById('holdings-list');
  list.replaceChildren();
  document.getElementById('holding-count').textContent = `${holdings.length} 个标的`;
  if (!holdings.length) {
    list.append(element('p', 'placeholder', '当前空仓，或等待第一个有效快照。'));
    return;
  }
  holdings.forEach((holding) => {
    const row = element('div', 'holding-row');
    const identity = element('div');
    identity.append(element('span', 'symbol', holding.symbol));
    identity.append(element('span', 'market', holding.market));

    const weight = element('div');
    const weightLabel = element('div', 'weight-label');
    weightLabel.append(element('span', '', 'NET WEIGHT'));
    weightLabel.append(element('span', '', signed(holding.weight_pct)));
    const track = element('div', 'weight-track');
    const fill = element('div', 'weight-fill');
    fill.style.width = `${Math.min(Math.abs(Number(holding.weight_pct)), 100)}%`;
    track.append(fill);
    weight.append(weightLabel, track);

    const pnl = element('div', 'pnl', signed(holding.unrealized_pnl_pct));
    applyTone(pnl, Number(holding.unrealized_pnl_pct));
    row.append(identity, weight, pnl);
    list.append(row);
  });
}

function renderEvents(events) {
  const list = document.getElementById('events-list');
  list.replaceChildren();
  if (!events.length) {
    list.append(element('p', 'placeholder', '还没有持仓变化记录。首次快照只建立基线，不算新交易。'));
    return;
  }
  [...events].reverse().forEach((event) => {
    const action = ACTION_LABELS[event.action] || '变化';
    const row = element('div', 'event-row');
    row.dataset.action = event.action;
    const body = element('div');
    const title = element('p', 'event-title');
    title.append(element('strong', '', event.symbol));
    title.append(element('span', 'event-action', action));
    const detail = element('div', 'event-detail', `${Number(event.previous_weight_pct).toFixed(2)}% → ${Number(event.weight_pct).toFixed(2)}%`);
    body.append(title, detail);
    row.append(element('span', 'event-dot'), body, element('time', 'event-time', formatTime(event.time)));
    list.append(row);
  });
}

function chartPointText(points) {
  document.getElementById('point-count').textContent = `${points.length} 个数据点`;
}

function renderChart(history) {
  const svg = document.getElementById('performance-chart');
  const empty = document.getElementById('chart-empty');
  const axis = document.getElementById('chart-axis');
  svg.replaceChildren();
  axis.replaceChildren();
  chartPointText(history);
  if (!history.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const points = history.filter((point) => Number.isFinite(Number(point.mii_index)));
  if (!points.length) return;
  const values = points.map((point) => Number(point.mii_index));
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * 0.18, 1);
  minimum -= padding;
  maximum += padding;
  const x = (index) => points.length === 1 ? 500 : index / (points.length - 1) * 1000;
  const y = (value) => 260 - (value - minimum) / (maximum - minimum) * 240;
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

  [20, 80, 140, 200, 260].forEach((lineY) => {
    const line = document.createElementNS(namespace, 'line');
    line.setAttribute('x1', '0');
    line.setAttribute('x2', '1000');
    line.setAttribute('y1', String(lineY));
    line.setAttribute('y2', String(lineY));
    line.setAttribute('class', 'chart-grid');
    svg.append(line);
  });

  const coordinates = points.map((point, index) => `${x(index)},${y(Number(point.mii_index))}`);
  if (points.length > 1) {
    const area = document.createElementNS(namespace, 'polygon');
    area.setAttribute('points', `0,280 ${coordinates.join(' ')} 1000,280`);
    area.setAttribute('class', 'chart-area');
    svg.append(area);
  }
  const line = document.createElementNS(namespace, 'polyline');
  line.setAttribute('points', coordinates.join(' '));
  line.setAttribute('class', 'chart-line');
  svg.append(line);
  const last = points[points.length - 1];
  const dot = document.createElementNS(namespace, 'circle');
  dot.setAttribute('cx', String(x(points.length - 1)));
  dot.setAttribute('cy', String(y(Number(last.mii_index))));
  dot.setAttribute('r', '5');
  dot.setAttribute('class', 'chart-point');
  svg.append(dot);

  [points[0].time, points[points.length - 1].time].forEach((time) => {
    axis.append(element('span', '', formatTime(time)));
  });
}

function updateFreshness(latest) {
  if (!latest.generated_at) {
    setStatus('loading', '等待首个快照');
    return;
  }
  const timestamp = new Date(latest.generated_at).getTime();
  if (!Number.isFinite(timestamp)) {
    setStatus('loading', '等待首个快照');
    return;
  }
  const age = Date.now() - timestamp;
  if (age > STALE_MS) {
    setStatus('stale', '数据可能过期');
  } else {
    setStatus('live', '近实时 · 自动刷新');
  }
}

function validatePayload(latest, history, events) {
  if (!latest || latest.schema_version !== 1 || !Array.isArray(latest.holdings)) {
    throw new Error('Invalid latest snapshot');
  }
  if (!Array.isArray(history) || !Array.isArray(events)) throw new Error('Invalid history');
}

async function fetchJson(name) {
  const response = await fetch(`${DATA_ROOT}/${name}.json?t=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  try {
    const [latest, history, events] = await Promise.all([
      fetchJson('latest'), fetchJson('history'), fetchJson('events'),
    ]);
    validatePayload(latest, history, events);
    renderMetrics(latest);
    renderHoldings(latest.holdings);
    renderEvents(events);
    renderChart(history);
    document.getElementById('updated-at').textContent = `数据时间 ${formatTime(latest.generated_at)} HKT · 每 60 秒检查更新`;
    updateFreshness(latest);
    hasSuccessfulLoad = true;
    document.getElementById('updated-at').dataset.timestamp = latest.generated_at;
  } catch (error) {
    console.warn('MII data refresh failed:', error.message);
    setStatus('error', hasSuccessfulLoad ? '刷新失败 · 展示上次数据' : '暂时无法读取数据');
    if (!hasSuccessfulLoad) {
      document.getElementById('updated-at').textContent = '公开数据尚未就绪，请稍后刷新';
    }
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
setInterval(() => {
  const timestamp = document.getElementById('updated-at').dataset.timestamp;
  if (hasSuccessfulLoad && timestamp) updateFreshness({ generated_at: timestamp });
}, 30_000);
