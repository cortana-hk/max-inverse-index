#!/usr/bin/env python3
"""Max Inverse Index 只读账户快照发布器。"""
import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from bisect import bisect_right
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIR = PROJECT_ROOT / 'private'
USD_HKD_RATE = 7.8
NEW_YORK = ZoneInfo('America/New_York')
HONG_KONG = ZoneInfo('Asia/Hong_Kong')
PUBLIC_FILENAMES = ('latest', 'history', 'events')
FORBIDDEN_PUBLIC_KEYS = {
    'quantity', 'qty', 'cost', 'market_price', 'market_value',
    'equity', 'equity_usd', 'cash', 'net_liquidation', 'account', 'account_id',
    'order', 'order_id', 'tiger_id', 'token', 'private_key', 'units', 'nav',
}
INSTANCE_ID_PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
SECRET_PATTERNS = (
    re.compile(r'github_pat_[A-Za-z0-9_]+'),
    re.compile(r'ghp_[A-Za-z0-9]+'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
)


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return _timestamp(value).isoformat(timespec='seconds')


def session_start(value):
    local_time = _timestamp(value).astimezone(NEW_YORK)
    boundary_date = local_time.date()
    if local_time.time() < datetime.strptime('20:00', '%H:%M').time():
        boundary_date -= timedelta(days=1)
    boundary = datetime.combine(boundary_date, datetime.strptime('20:00', '%H:%M').time(), NEW_YORK)
    return boundary.astimezone(timezone.utc)


def _refresh_session_day_changes(history):
    times = [_timestamp(point['time']) for point in history]
    first_in_session = {}
    for index, point in enumerate(history):
        start = session_start(times[index])
        first_in_session.setdefault(start, index)
        baseline_index = bisect_right(times, start, 0, index) - 1
        if baseline_index < 0:
            baseline_index = first_in_session[start]
        baseline = _finite(history[baseline_index]['mii_index'], '日界 MII 点位')
        current = _finite(point['mii_index'], '当期 MII 点位')
        point['day_return_pct'] = _public_return(current / baseline - 1.0) if baseline > 0 else None


def _finite(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} 不是数值') from exc
    if not math.isfinite(number):
        raise ValueError(f'{label} 不是有限数值')
    return number


def _public_return(value):
    return None if value is None else round(value * 100.0, 4)


def _normalise_positions(positions, equity_usd):
    normalised = []
    for item in positions or []:
        contract = getattr(item, 'contract', None)
        symbol = str(getattr(contract, 'symbol', '') or '').strip().upper()
        if not symbol:
            raise ValueError('持仓缺少标的代码')
        quantity = _finite(getattr(item, 'quantity', None), f'{symbol} 持仓数量')
        if quantity == 0:
            continue
        price = _finite(getattr(item, 'market_price', None), f'{symbol} 行情')
        if price <= 0:
            raise ValueError(f'{symbol} 行情无效')
        currency = str(
            getattr(contract, 'currency', None) or getattr(item, 'currency', None) or 'USD'
        ).upper()
        market_value = getattr(item, 'market_value', None)
        if market_value is None:
            market_value = quantity * price
        market_value = _finite(market_value, f'{symbol} 市值')
        market_value_usd = market_value / USD_HKD_RATE if currency == 'HKD' else market_value
        average_cost = _finite(getattr(item, 'average_cost', None), f'{symbol} 成本')
        supplied_pnl = getattr(item, 'unrealized_pnl_percent', None)
        if supplied_pnl is not None:
            pnl_pct = _finite(supplied_pnl, f'{symbol} 浮动盈亏') * 100.0
        elif average_cost > 0:
            pnl_pct = ((average_cost - price) if quantity < 0 else (price - average_cost)) / average_cost * 100
        else:
            pnl_pct = 0.0
        normalised.append({
            'symbol': symbol,
            'market': str(getattr(contract, 'market', None) or ('HK' if currency == 'HKD' else 'US')),
            'currency': currency,
            'quantity': quantity,
            'weight_pct': round(market_value_usd / equity_usd * 100.0, 4),
            'average_cost': round(average_cost, 4),
            'current_price': round(price, 4),
            'unrealized_pnl_pct': round(pnl_pct, 4),
        })
    return sorted(normalised, key=lambda item: (-abs(item['weight_pct']), item['symbol']))


def _migrate_legacy_index(state):
    if not state.get('history') or state.get('index_mode') == 'inverse':
        return
    previous_account_index = None
    inverse_index = 100.0
    for point in state['history']:
        account_index = _finite(point['mii_index'], '旧指数点位')
        if previous_account_index and previous_account_index > 0:
            interval_return = account_index / previous_account_index - 1.0
            inverse_index = max(0.0, inverse_index * (1.0 - interval_return))
        point.update({
            'mii_index': round(inverse_index, 4),
            'total_return_pct': _public_return(inverse_index / 100.0 - 1.0),
        })
        previous_account_index = account_index
    _refresh_session_day_changes(state['history'])
    state['mii_index'] = inverse_index
    state['index_mode'] = 'inverse'
    if state.get('latest'):
        for key in ('mii_index', 'total_return_pct', 'day_return_pct'):
            state['latest'][key] = state['history'][-1][key]


def _position_events(previous, current, observed_at):
    events = []
    previous_by_symbol = {item['symbol']: item for item in previous}
    current_by_symbol = {item['symbol']: item for item in current}
    for symbol in sorted(set(previous_by_symbol) | set(current_by_symbol)):
        before = previous_by_symbol.get(symbol)
        after = current_by_symbol.get(symbol)
        if before is None:
            action = 'new'
        elif after is None:
            action = 'close'
        elif before['quantity'] * after['quantity'] < 0:
            action = 'new'
        elif abs(after['quantity']) > abs(before['quantity']):
            action = 'increase'
        elif abs(after['quantity']) < abs(before['quantity']):
            action = 'reduce'
        else:
            continue
        events.append({
            'time': _iso(observed_at),
            'symbol': symbol,
            'action': action,
            'previous_weight_pct': before['weight_pct'] if before else 0.0,
            'weight_pct': after['weight_pct'] if after else 0.0,
        })
    return events


def advance_state(state, positions, equity_usd, observed_at, cash_flows=None):
    """推进私密收益状态并返回仅含白名单字段的公开文件。"""
    equity_usd = _finite(equity_usd, '账户净资产')
    if equity_usd <= 0:
        raise ValueError('账户净资产必须为正数')
    observed_at = _timestamp(observed_at)
    next_state = copy.deepcopy(state or {})
    _migrate_legacy_index(next_state)
    current = _normalise_positions(positions, equity_usd)
    previous = next_state.get('positions', [])
    had_baseline = bool(next_state.get('history'))
    daily_nav = next_state.setdefault('daily_nav', {})
    flows = sorted(cash_flows or [], key=lambda item: _timestamp(item['time']))
    processed = set(next_state.get('processed_cash_flows', []))

    if not had_baseline:
        next_state['units'] = equity_usd
        next_state['nav'] = 1.0
        next_state['mii_index'] = 100.0
        next_state['index_mode'] = 'inverse'
        for flow in flows:
            if _timestamp(flow['time']) <= observed_at:
                processed.add(str(flow['id']))
    else:
        previous_nav = _finite(next_state['nav'], '上一单位净值')
        previous_mii = _finite(next_state.get('mii_index', 100.0), '上一 MII 点位')
        units = _finite(next_state['units'], '上一基金份额')
        for flow in flows:
            flow_id = str(flow['id'])
            if flow_id in processed or _timestamp(flow['time']) > observed_at:
                continue
            amount = _finite(flow['amount_usd'], '出入金金额')
            units += amount / previous_nav
            if units <= 0:
                raise ValueError('出入金后基金份额必须为正数')
            processed.add(flow_id)
        next_state['units'] = units
        next_state['nav'] = equity_usd / units
        interval_return = next_state['nav'] / previous_nav - 1.0
        next_state['mii_index'] = max(0.0, previous_mii * (1.0 - interval_return))

    nav = next_state['nav']
    mii_index = next_state['mii_index']
    latest = {
        'schema_version': 1,
        'generated_at': _iso(observed_at),
        'mii_index': round(mii_index, 4),
        'total_return_pct': _public_return(mii_index / 100.0 - 1.0),
        'day_return_pct': None,
        'holdings': [{
            'symbol': item['symbol'],
            'market': item['market'],
            'currency': item['currency'],
            'weight_pct': item['weight_pct'],
            'average_cost': item['average_cost'],
            'current_price': item['current_price'],
            'unrealized_pnl_pct': item['unrealized_pnl_pct'],
        } for item in current],
    }
    point = {key: latest[key] for key in (
        'generated_at', 'mii_index', 'total_return_pct', 'day_return_pct')}
    point['time'] = point.pop('generated_at')
    history = next_state.get('history', []) + [point]
    _refresh_session_day_changes(history)
    latest['day_return_pct'] = history[-1]['day_return_pct']
    point['day_return_pct'] = history[-1]['day_return_pct']
    events = next_state.get('events', [])
    if had_baseline:
        events = events + _position_events(previous, current, observed_at)

    next_state.update({
        'positions': current,
        'processed_cash_flows': sorted(processed),
        'history': history,
        'events': events,
        'latest': latest,
        'day_boundary': 'new_york_20',
    })
    daily_nav[observed_at.astimezone(NEW_YORK).date().isoformat()] = nav

    previous_latest = state.get('latest') if state else None
    if previous_latest:
        old_public = {key: value for key, value in previous_latest.items() if key != 'generated_at'}
        new_public = {key: value for key, value in latest.items() if key != 'generated_at'}
        previous_time = _timestamp(previous_latest['generated_at'])
        no_new_events = len(next_state['events']) == len(state.get('events', []))
        if (old_public == new_public and no_new_events and state.get('day_boundary') == 'new_york_20'
                and (observed_at - previous_time).total_seconds() < 300):
            next_state['latest'] = previous_latest
            next_state['history'] = copy.deepcopy(state.get('history', []))

    files = {
        'latest': copy.deepcopy(next_state['latest']),
        'history': copy.deepcopy(next_state['history']),
        'events': copy.deepcopy(next_state['events']),
    }
    assert_public_payload_safe(files)
    return next_state, files


def read_asset_history(trade):
    end_date = datetime.now(NEW_YORK).date().isoformat()
    result = trade.get_analytics_asset(start_date='2000-01-01', end_date=end_date) or {}
    records = []
    for item in result.get('history', []):
        asset = _finite(item['asset'], '历史净资产')
        if asset <= 0:
            continue
        timestamp = datetime.fromtimestamp(item['date'] / 1000, timezone.utc).isoformat(timespec='seconds')
        records.append({
            'time': timestamp,
            'asset': asset,
            'amount_usd': _finite(item.get('deposit', 0), '历史入金') - _finite(item.get('withdrawal', 0), '历史出金'),
        })
    return sorted(records, key=lambda record: _timestamp(record['time']))


def backfill_state_from_assets(state, records):
    if not records:
        return copy.deepcopy(state)
    rebuilt = {}
    for record in sorted(records, key=lambda item: _timestamp(item['time'])):
        amount = _finite(record.get('amount_usd', 0), '历史出入金')
        flows = []
        if amount:
            flows = [{
                'id': f"tiger-asset-{record['time']}", 'time': record['time'], 'amount_usd': amount,
            }]
        rebuilt, _ = advance_state(rebuilt, [], record['asset'], record['time'], flows)

    last_asset_time = _timestamp(rebuilt['history'][-1]['time'])
    live_points = [point for point in state.get('history', []) if _timestamp(point['time']) > last_asset_time]
    if live_points:
        first_live_index = _finite(live_points[0]['mii_index'], '盘中指数点位')
        scale = rebuilt['mii_index'] / first_live_index
        base_nav = rebuilt['nav']
        for point in live_points:
            old_index = _finite(point['mii_index'], '盘中指数点位')
            new_index = old_index * scale
            rebuilt['nav'] = base_nav * first_live_index / old_index
            rebuilt['history'].append({
                'time': point['time'],
                'mii_index': round(new_index, 4),
                'total_return_pct': _public_return(new_index / 100.0 - 1.0),
                'day_return_pct': None,
            })
            rebuilt['daily_nav'][_timestamp(point['time']).astimezone(NEW_YORK).date().isoformat()] = rebuilt['nav']
        rebuilt['mii_index'] = _finite(rebuilt['history'][-1]['mii_index'], '最新指数点位')

    rebuilt['positions'] = copy.deepcopy(state.get('positions', []))
    rebuilt['events'] = copy.deepcopy(state.get('events', []))
    rebuilt['processed_cash_flows'] = sorted(set(rebuilt.get('processed_cash_flows', [])) | set(state.get('processed_cash_flows', [])))
    for key in ('fixture_cursor',):
        if key in state:
            rebuilt[key] = state[key]
    _refresh_session_day_changes(rebuilt['history'])
    latest_point = rebuilt['history'][-1]
    rebuilt['latest'] = {
        'schema_version': 1,
        'generated_at': latest_point['time'],
        'mii_index': latest_point['mii_index'],
        'total_return_pct': latest_point['total_return_pct'],
        'day_return_pct': latest_point['day_return_pct'],
        'holdings': copy.deepcopy(state.get('latest', {}).get('holdings', [])),
    }
    return rebuilt


def read_fill_events(path):
    """从本地成交记录回填全部订单，仅输出时间、方向、价格和币种。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open('r', encoding='utf-8') as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    directions = {}
    for record in records:
        if record.get('record_type') == 'buy_components':
            directions[str(record.get('order_id', ''))] = 'buy'
        elif record.get('record_type') == 'sell_components':
            directions[str(record.get('order_id', ''))] = 'sell'

    orders = {}
    for line_number, record in enumerate(records):
        if record.get('record_type') != 'fill':
            continue
        order_id = str(record.get('order_id', ''))
        key = order_id or f'line-{line_number}'
        quantity = _finite(record.get('qty'), '成交数量')
        price = _finite(record.get('price'), '成交价格')
        if quantity == 0 or price <= 0:
            continue
        symbol = str(record.get('symbol', '')).upper()
        if not symbol:
            raise ValueError('成交记录缺少标的代码')
        parsed_time = datetime.fromisoformat(str(record['time']).replace('Z', '+00:00'))
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=HONG_KONG)
        timestamp = parsed_time.astimezone(timezone.utc)
        order = orders.setdefault(key, {
            'symbol': symbol, 'price_quantity': 0.0, 'quantity': 0.0,
            'time': timestamp, 'action': directions.get(order_id, 'buy' if quantity > 0 else 'sell'),
        })
        order['price_quantity'] += price * abs(quantity)
        order['quantity'] += abs(quantity)
        order['time'] = max(order['time'], timestamp)

    events = [{
        'time': order['time'].isoformat(timespec='seconds'),
        'symbol': order['symbol'],
        'action': order['action'],
        'price': round(order['price_quantity'] / order['quantity'], 4),
        'currency': 'HKD' if order['symbol'].isdigit() else 'USD',
    } for order in orders.values()]
    return sorted(events, key=lambda event: event['time'])


def merge_order_events(*event_groups):
    merged = {}
    for events in event_groups:
        for event in events:
            key = (
                event['time'], event['symbol'], event['action'],
                round(float(event['price']), 4), event['currency'],
            )
            merged[key] = event
    return sorted(merged.values(), key=lambda event: event['time'])


def read_broker_order_events(trade, start_time=None, page_size=100):
    events = []
    seen_order_ids = set()
    end_time = None
    for _ in range(1000):
        batch = trade.get_orders(limit=page_size, start_time=start_time, end_time=end_time) or []
        if not batch:
            break
        for order in batch:
            order_id = str(getattr(order, 'id', None) or getattr(order, 'order_id', ''))
            if order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            status = str(getattr(order, 'status', getattr(order, '_status', ''))).split('.')[-1].upper()
            if status not in {'FILLED', 'PARTIALLY_FILLED', 'PARTIAL_FILLED'}:
                continue
            price = _finite(getattr(order, 'avg_fill_price', None), '订单成交均价')
            if price <= 0:
                continue
            contract = order.contract
            symbol = str(contract.symbol).upper()
            action = str(order.action).split('.')[-1].upper()
            timestamp = datetime.fromtimestamp(order.order_time / 1000, timezone.utc).isoformat(timespec='seconds')
            events.append({
                'time': timestamp,
                'symbol': symbol,
                'action': 'sell' if action.startswith('SELL') else 'buy',
                'price': round(price, 4),
                'currency': str(getattr(contract, 'currency', None) or ('HKD' if symbol.isdigit() else 'USD')).upper(),
            })
        if len(batch) < page_size:
            break
        end_time = min(order.order_time for order in batch) - 1
    return merge_order_events(events)


def sync_broker_orders(trade, private_dir):
    cache_path = Path(private_dir) / 'broker_orders.json'
    cache = _load_json(cache_path, {'last_time': 0, 'events': []})
    start_time = int(cache.get('last_time', 0)) + 1 if cache.get('last_time') else None
    new_events = read_broker_order_events(trade, start_time=start_time)
    events = merge_order_events(cache.get('events', []), new_events)
    last_time = max([int(cache.get('last_time', 0))] + [
        int(_timestamp(event['time']).timestamp() * 1000) for event in new_events
    ])
    _atomic_json(cache_path, {'last_time': last_time, 'events': events})
    return events


def extract_net_liquidation(assets):
    for account in assets or []:
        summary = getattr(account, 'summary', None)
        value = getattr(summary, 'net_liquidation', None)
        if value is not None:
            equity = _finite(value, '账户净资产')
            if equity > 0:
                return equity
    raise ValueError('未读取到有效账户净资产')


def create_trade_client(config_dir):
    config = TigerOpenClientConfig(props_path=str(Path(config_dir)))
    config.token_refresh_duration = 24 * 60 * 60
    return TradeClient(config)


def collect_account(trade):
    positions = trade.get_positions() or []
    equity = extract_net_liquidation(trade.get_assets())
    return positions, equity


def assert_public_payload_safe(files):
    if set(files) != set(PUBLIC_FILENAMES):
        raise ValueError('公开文件集合不完整')

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in FORBIDDEN_PUBLIC_KEYS:
                    raise ValueError(f'公开数据包含禁止字段: {key}')
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            if any(pattern.search(value) for pattern in SECRET_PATTERNS):
                raise ValueError('公开数据包含疑似密钥')

    walk(files)


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _git(repo, *args, check=True):
    result = subprocess.run(
        ['git', *args], cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f'Git 命令失败: git {args[0]}')
    return result


def publish_files(repo, files, push=True, instance_id=None, registry=None):
    """发布单个实例，公开数据只写入该实例自己的目录。"""
    if not instance_id or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise ValueError('实例 ID 无效')
    repo = Path(repo).resolve()
    assert_public_payload_safe(files)
    for name in PUBLIC_FILENAMES:
        _atomic_json(repo / 'data' / 'instances' / instance_id / f'{name}.json', files[name])
    if registry is not None:
        _atomic_json(repo / 'data' / 'instances.json', registry)
    _git(repo, 'add', '--', 'data')
    if _git(repo, 'diff', '--cached', '--quiet', '--', 'data', check=False).returncode == 0:
        return False
    _git(repo, 'commit', '-m', 'Update MII public snapshot', '--', 'data')
    if push:
        pushed = _git(repo, 'push', 'origin', 'main', check=False)
        if pushed.returncode:
            _git(repo, 'pull', '--rebase', 'origin', 'main')
            _git(repo, 'push', 'origin', 'main')
    return True


def load_instances(path):
    config = _load_json(Path(path), None)
    if not isinstance(config, dict):
        raise ValueError('实例配置不存在或格式无效')
    public_registry(config)
    return config


def public_registry(config):
    instances = config.get('instances', [])
    if not instances:
        raise ValueError('至少需要一个账号实例')
    public_instances = []
    seen = set()
    for instance in instances:
        if not instance.get('enabled', True):
            continue
        instance_id = str(instance.get('id', ''))
        name = str(instance.get('name', '')).strip()
        if not INSTANCE_ID_PATTERN.fullmatch(instance_id):
            raise ValueError(f'实例 ID 无效: {instance_id}')
        if not name or instance_id in seen:
            raise ValueError('实例名称不能为空且实例 ID 不能重复')
        seen.add(instance_id)
        public_instance = {'id': instance_id, 'name': name}
        if instance.get('source', 'tiger') == 'fixture':
            public_instance['test'] = True
        public_instances.append(public_instance)
    default_instance = config.get('default_instance')
    if default_instance not in seen:
        raise ValueError('默认实例不存在')
    return {
        'schema_version': 1,
        'default_instance': default_instance,
        'instances': public_instances,
    }


def select_instances(config, requested=None):
    public_registry(config)
    enabled = [instance for instance in config['instances'] if instance.get('enabled', True)]
    if requested is None:
        return enabled
    selected = [instance for instance in enabled if instance['id'] == requested]
    if not selected:
        raise ValueError(f'实例不存在或未启用: {requested}')
    return selected


def _load_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    with path.open('r', encoding='utf-8') as stream:
        return json.load(stream)


def _save_private_json(path, payload):
    _atomic_json(path, payload)


@contextmanager
def _instance_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        yield True
    finally:
        try:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        stream.close()


def _instance_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_fixture_snapshot(path, cursor):
    payload = _load_json(Path(path), {})
    snapshots = payload.get('snapshots', [])
    if not snapshots:
        raise ValueError('测试数据源缺少 snapshots')
    cursor = max(0, int(cursor))
    index = min(cursor, len(snapshots) - 1)
    snapshot = snapshots[index]
    positions = []
    for item in snapshot.get('positions', []):
        currency = str(item.get('currency', 'USD')).upper()
        quantity = item['quantity']
        price = item['price']
        positions.append(SimpleNamespace(
            contract=SimpleNamespace(
                symbol=item['symbol'], market=item.get('market', 'US'), currency=currency),
            quantity=quantity,
            average_cost=item['average_cost'],
            market_price=price,
            market_value=quantity * price,
            unrealized_pnl_percent=None,
        ))
    return positions, snapshot['equity'], index + 1


def initialise_instance_resources(instance, tiger_config=None, fills_file=None):
    instance_id = instance['id']
    if not INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise ValueError('实例 ID 无效')
    private_dir = PRIVATE_DIR / 'instances' / instance_id
    private_dir.mkdir(parents=True, exist_ok=True)
    for filename, default in (('private_state.json', {}), ('cash_flows.json', [])):
        path = private_dir / filename
        if not path.exists():
            _atomic_json(path, default)

    instance_root = private_dir.resolve()
    config_value = tiger_config or instance.get('tiger_config')
    if config_value:
        config_path = _instance_path(config_value).resolve()
        if config_path.is_relative_to(instance_root):
            config_path.mkdir(parents=True, exist_ok=True)
    resolved_fills = _instance_path(fills_file or instance['fills_file']).resolve()
    if resolved_fills.is_relative_to(instance_root):
        resolved_fills.parent.mkdir(parents=True, exist_ok=True)
        resolved_fills.touch(exist_ok=True)
    return private_dir, resolved_fills


def run_instance(instance, args, registry):
    instance_id = instance['id']
    tiger_config = args.tiger_config or instance.get('tiger_config')
    private_dir, fills_path = initialise_instance_resources(
        instance, tiger_config=tiger_config, fills_file=args.fills_file)
    state = _load_json(private_dir / 'private_state.json', {})
    broker_events = []
    source = instance.get('source', 'tiger')
    if source == 'fixture':
        positions, equity, fixture_cursor = load_fixture_snapshot(
            _instance_path(instance['fixture_file']), state.get('fixture_cursor', 0))
    elif source == 'tiger':
        tiger_config = args.tiger_config or instance['tiger_config']
        trade = create_trade_client(_instance_path(tiger_config))
        positions, equity = collect_account(trade)
        fixture_cursor = None
        try:
            state = backfill_state_from_assets(state, read_asset_history(trade))
        except Exception as exc:
            print(f"MII[{instance_id}] 历史资产补齐失败，保留现有历史：{type(exc).__name__}", file=sys.stderr)
        try:
            broker_events = sync_broker_orders(trade, private_dir)
        except Exception as exc:
            print(f"MII[{instance_id}] 历史订单补齐失败，使用本地记录：{type(exc).__name__}", file=sys.stderr)
            broker_events = _load_json(private_dir / 'broker_orders.json', {'events': []}).get('events', [])
    else:
        raise ValueError(f'未知实例数据源: {source}')
    observed_at = datetime.now(timezone.utc)
    flows = _load_json(private_dir / 'cash_flows.json', [])
    next_state, files = advance_state(state, positions, equity, observed_at, flows)
    if fixture_cursor is not None:
        next_state['fixture_cursor'] = fixture_cursor
    fill_events = read_fill_events(fills_path)
    if broker_events or fill_events:
        files['events'] = merge_order_events(broker_events, fill_events)
    assert_public_payload_safe(files)

    if args.dry_run:
        _atomic_json(private_dir / 'dry-run-public.json', files)
        print(f"MII[{instance_id}] dry-run 成功：{len(files['latest']['holdings'])} 个持仓。")
        return
    publish_files(Path(args.repo), files, push=not args.no_push, instance_id=instance_id, registry=registry)
    _save_private_json(private_dir / 'private_state.json', next_state)
    print(f"MII[{instance_id}] 快照已发布：{observed_at.isoformat(timespec='seconds')}")


def run_once(args):
    if args.instance is None and (args.tiger_config or args.fills_file):
        raise ValueError('覆盖账号配置时必须同时指定 --instance')
    config = load_instances(Path(args.instances_config))
    selected = select_instances(config, args.instance)
    registry = public_registry(config)
    errors = []
    for instance in selected:
        try:
            run_instance(instance, args, registry)
        except Exception as exc:
            errors.append(instance['id'])
            print(f"MII[{instance['id']}] 本轮失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    if len(errors) == len(selected):
        raise RuntimeError('所有选中实例均发布失败')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='发布 Max Inverse Index 脱敏持仓快照')
    parser.add_argument('--repo', default=str(PROJECT_ROOT))
    parser.add_argument('--instances-config', default=os.environ.get(
        'MII_INSTANCES_CONFIG', str(PRIVATE_DIR / 'instances.json')))
    parser.add_argument('--instance', help='只运行指定实例；默认运行全部启用实例')
    parser.add_argument('--tiger-config', default=os.environ.get('MII_TIGER_CONFIG_DIR'),
                        help='覆盖指定实例的 Tiger 配置目录')
    parser.add_argument('--fills-file', default=os.environ.get('MII_FILLS_FILE'),
                        help='覆盖指定实例的成交记录文件')
    parser.add_argument('--once', action='store_true', help='只执行一次')
    parser.add_argument('--dry-run', action='store_true', help='只验证采集，不写公开仓库或收益基线')
    parser.add_argument('--no-push', action='store_true', help='只提交到本地公开仓库镜像')
    parser.add_argument('--interval', type=int, default=60, help='持续模式采集间隔秒数')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.interval < 15:
        raise SystemExit('--interval 不能小于 15 秒')
    lock_path = PRIVATE_DIR / 'publisher.lock'
    with _instance_lock(lock_path) as acquired:
        if not acquired:
            print('MII 发布器已在运行。')
            return 0
        if args.once or args.dry_run:
            run_once(args)
            return 0
        while True:
            try:
                run_once(args)
            except Exception as exc:
                print(f'MII 本轮发布失败：{type(exc).__name__}: {exc}', file=sys.stderr)
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
