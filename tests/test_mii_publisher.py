# -*- coding: utf-8 -*-
"""MII 反向收益、订单回填、公开价格和安全测试（不访问网络）"""
import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from publisher.mii_publisher import (
    advance_state,
    assert_public_payload_safe,
    collect_account,
    extract_net_liquidation,
    load_fixture_snapshot,
    load_instances,
    public_registry,
    publish_files,
    read_fill_events,
    run_instance,
    select_instances,
    session_start,
)


def position(symbol='AAPL', qty=10, cost=90.0, price=100.0, currency='USD', pnl=None):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, market='HK' if currency == 'HKD' else 'US', currency=currency),
        quantity=qty,
        average_cost=cost,
        market_price=price,
        market_value=qty * price,
        unrealized_pnl_percent=pnl,
    )


class SnapshotTests(unittest.TestCase):
    def test_first_snapshot_establishes_baseline_without_events(self):
        state, files = advance_state({}, [position()], 1000.0, '2026-01-01T12:00:00+00:00')

        self.assertEqual(files['latest']['mii_index'], 100.0)
        self.assertEqual(files['latest']['total_return_pct'], 0.0)
        self.assertEqual(files['latest']['day_return_pct'], 0.0)
        self.assertEqual(files['events'], [])
        self.assertEqual(files['latest']['holdings'], [{
            'symbol': 'AAPL', 'market': 'US', 'currency': 'USD', 'weight_pct': 100.0,
            'average_cost': 90.0, 'current_price': 100.0, 'unrealized_pnl_pct': 11.1111,
        }])
        self.assertEqual(state['units'], 1000.0)
        self.assertEqual(state['index_mode'], 'inverse')
        self.assertNotIn('quantity', json.dumps(files))
        self.assertNotIn('market_value', json.dumps(files))

    def test_inverse_index_moves_opposite_to_each_account_interval(self):
        state, _ = advance_state({}, [], 1000.0, '2026-01-01T12:00:00+00:00')
        state, files = advance_state(state, [], 1100.0, '2026-01-01T13:00:00+00:00')
        self.assertEqual(files['latest']['total_return_pct'], -10.0)
        self.assertEqual(files['latest']['mii_index'], 90.0)

        state, files = advance_state(state, [], 880.0, '2026-01-01T14:00:00+00:00')
        self.assertEqual(files['latest']['total_return_pct'], 8.0)
        self.assertEqual(files['latest']['mii_index'], 108.0)

    def test_deposits_and_withdrawals_do_not_distort_inverse_returns(self):
        state, _ = advance_state({}, [], 1000.0, '2026-01-01T12:00:00+00:00')
        deposit = {'id': 'd1', 'time': '2026-01-01T13:00:00+00:00', 'amount_usd': 500.0}
        state, files = advance_state(
            state, [], 1500.0, '2026-01-01T13:01:00+00:00', [deposit])
        self.assertEqual(files['latest']['total_return_pct'], 0.0)
        self.assertEqual(state['units'], 1500.0)

        state, files = advance_state(state, [], 1650.0, '2026-01-01T14:00:00+00:00', [deposit])
        self.assertEqual(files['latest']['total_return_pct'], -10.0)
        withdrawal = {'id': 'w1', 'time': '2026-01-01T15:00:00+00:00', 'amount_usd': -550.0}
        state, files = advance_state(
            state, [], 1100.0, '2026-01-01T15:01:00+00:00', [deposit, withdrawal])
        self.assertEqual(files['latest']['total_return_pct'], -10.0)
        self.assertEqual(state['units'], 1000.0)

    def test_day_change_uses_new_york_after_hours_close(self):
        state, _ = advance_state({}, [], 1000.0, '2026-07-01T00:00:00+00:00')
        state, files = advance_state(state, [], 1050.0, '2026-07-01T12:00:00+00:00')
        self.assertEqual(files['latest']['day_return_pct'], -5.0)
        state, files = advance_state(state, [], 1155.0, '2026-07-02T00:01:00+00:00')
        self.assertEqual(files['latest']['day_return_pct'], -10.0)
        self.assertEqual(files['latest']['total_return_pct'], -14.5)

    def test_day_change_uses_first_available_point_when_history_starts_intraday(self):
        state, files = advance_state({}, [], 1000.0, '2026-07-01T12:00:00+00:00')
        self.assertEqual(files['latest']['day_return_pct'], 0.0)
        state, files = advance_state(state, [], 1050.0, '2026-07-01T13:00:00+00:00')
        self.assertEqual(files['latest']['day_return_pct'], -5.0)

    def test_session_start_handles_dst_and_after_hours_boundary(self):
        self.assertEqual(session_start('2026-07-01T19:30:00-04:00').isoformat(), '2026-07-01T00:00:00+00:00')
        self.assertEqual(session_start('2026-07-01T20:30:00-04:00').isoformat(), '2026-07-02T00:00:00+00:00')
        self.assertEqual(session_start('2026-01-05T19:30:00-05:00').isoformat(), '2026-01-05T01:00:00+00:00')
        self.assertEqual(session_start('2026-01-05T20:30:00-05:00').isoformat(), '2026-01-06T01:00:00+00:00')

    def test_legacy_normal_index_history_is_migrated_to_inverse(self):
        legacy = {
            'units': 1000.0,
            'nav': 1.1,
            'positions': [],
            'processed_cash_flows': [],
            'daily_nav': {'2026-01-01': 1.1},
            'history': [
                {'time': '2026-01-01T12:00:00+00:00', 'mii_index': 100.0, 'total_return_pct': 0.0, 'day_return_pct': None},
                {'time': '2026-01-01T13:00:00+00:00', 'mii_index': 110.0, 'total_return_pct': 10.0, 'day_return_pct': None},
            ],
            'events': [],
            'latest': {
                'schema_version': 1, 'generated_at': '2026-01-01T13:00:00+00:00',
                'mii_index': 110.0, 'total_return_pct': 10.0, 'day_return_pct': None, 'holdings': [],
            },
        }
        state, files = advance_state(legacy, [], 1210.0, '2026-01-01T14:00:00+00:00')
        self.assertEqual([point['mii_index'] for point in files['history']], [100.0, 90.0, 81.0])
        self.assertEqual(files['latest']['total_return_pct'], -19.0)
        self.assertEqual(state['index_mode'], 'inverse')

    def test_position_changes_emit_only_weight_based_events(self):
        state, _ = advance_state({}, [position(qty=10)], 1000.0, '2026-01-01T12:00:00+00:00')
        expected = [(15, 'increase'), (10, 'reduce')]
        for index, (qty, action) in enumerate(expected, 1):
            state, files = advance_state(
                state, [position(qty=qty)], 1000.0, f'2026-01-01T1{index}:00:00+00:00')
            self.assertEqual(files['events'][-1]['action'], action)
            self.assertNotIn('quantity', files['events'][-1])

        state, files = advance_state(state, [], 1000.0, '2026-01-01T14:00:00+00:00')
        self.assertEqual(files['events'][-1]['action'], 'close')
        self.assertEqual(files['events'][-1]['weight_pct'], 0.0)

        state, files = advance_state(
            state, [position(symbol='TSLA')], 1000.0, '2026-01-01T15:00:00+00:00')
        self.assertEqual(files['events'][-1]['action'], 'new')
        self.assertEqual(files['events'][-1]['previous_weight_pct'], 0.0)

    def test_tiger_pnl_ratio_is_converted_to_percentage(self):
        _, files = advance_state(
            {}, [position(pnl=0.0276)], 1000.0, '2026-01-01T12:00:00+00:00')
        self.assertEqual(files['latest']['holdings'][0]['unrealized_pnl_pct'], 2.76)

    def test_multi_currency_weights_prices_and_short_pnl(self):
        hk = position(symbol='00700', qty=100, cost=70.0, price=78.0, currency='HKD')
        short = position(symbol='TSLA', qty=-10, cost=100.0, price=90.0)
        _, files = advance_state({}, [hk, short], 2000.0, '2026-01-01T12:00:00+00:00')
        holdings = {item['symbol']: item for item in files['latest']['holdings']}
        self.assertEqual(holdings['00700']['weight_pct'], 50.0)
        self.assertEqual(holdings['00700']['currency'], 'HKD')
        self.assertEqual(holdings['00700']['average_cost'], 70.0)
        self.assertEqual(holdings['00700']['current_price'], 78.0)
        self.assertEqual(holdings['TSLA']['unrealized_pnl_pct'], 10.0)

    def test_empty_positions_are_valid(self):
        _, files = advance_state({}, [], 1000.0, '2026-01-01T12:00:00+00:00')
        self.assertEqual(files['latest']['holdings'], [])

    def test_invalid_equity_or_quote_fails_closed(self):
        for equity in (0, -1, math.nan, math.inf):
            with self.subTest(equity=equity), self.assertRaises(ValueError):
                advance_state({}, [], equity, '2026-01-01T12:00:00+00:00')
        for price in (0, -1, math.nan, math.inf):
            with self.subTest(price=price), self.assertRaises(ValueError):
                advance_state({}, [position(price=price)], 1000.0, '2026-01-01T12:00:00+00:00')

    def test_history_and_position_events_are_not_truncated(self):
        state = {}
        for index in range(7):
            qty = 10 + index % 2
            state, files = advance_state(
                state, [position(qty=qty)], 1000.0 + index,
                f'2026-01-01T12:0{index}:00+00:00')
        self.assertEqual(len(files['history']), 7)
        self.assertEqual(len(files['events']), 6)
        self.assertEqual(files['history'][0]['time'], '2026-01-01T12:00:00+00:00')

    def test_unchanged_public_data_waits_for_heartbeat(self):
        state, first = advance_state({}, [], 1000.0, '2026-01-01T12:00:00+00:00')
        state, second = advance_state(state, [], 1000.0, '2026-01-01T12:01:00+00:00')
        self.assertEqual(second, first)

        state, heartbeat = advance_state(state, [], 1000.0, '2026-01-01T12:06:00+00:00')
        self.assertEqual(heartbeat['latest']['generated_at'], '2026-01-01T12:06:00+00:00')
        self.assertEqual(len(heartbeat['history']), 2)

    def test_input_state_is_not_mutated(self):
        original = {}
        state, _ = advance_state(original, [], 1000.0, '2026-01-01T12:00:00+00:00')
        self.assertEqual(original, {})
        snapshot = copy.deepcopy(state)
        advance_state(state, [], 1001.0, '2026-01-01T13:00:00+00:00')
        self.assertEqual(state, snapshot)


class FillEventTests(unittest.TestCase):
    def test_fill_history_is_backfilled_grouped_and_sanitised(self):
        records = [
            {'record_type': 'buy_components', 'order_id': 'b1'},
            {'record_type': 'fill', 'order_id': 'b1', 'symbol': 'AAPL', 'price': 100, 'qty': 2, 'time': '2026-01-01T12:00:00'},
            {'record_type': 'fill', 'order_id': 'b1', 'symbol': 'AAPL', 'price': 110, 'qty': 2, 'time': '2026-01-01T12:01:00'},
            {'record_type': 'sell_components', 'order_id': 's1'},
            {'record_type': 'fill', 'order_id': 's1', 'symbol': '00700', 'price': 78, 'qty': 100, 'time': '2026-01-01T12:02:00'},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fills.jsonl'
            path.write_text('\n'.join(json.dumps(record) for record in records) + '\n', encoding='utf-8')
            events = read_fill_events(path)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], {
            'time': '2026-01-01T04:01:00+00:00', 'symbol': 'AAPL', 'action': 'buy',
            'price': 105.0, 'currency': 'USD',
        })
        self.assertEqual(events[1]['action'], 'sell')
        self.assertEqual(events[1]['currency'], 'HKD')
        self.assertNotIn('qty', json.dumps(events))
        self.assertNotIn('order_id', json.dumps(events))

    def test_missing_fill_file_returns_empty_history(self):
        self.assertEqual(read_fill_events(Path('/unlikely/mii-missing-fills.jsonl')), [])


class AccountAdapterTests(unittest.TestCase):
    def test_publisher_has_no_python_dependency_on_tiger_project(self):
        source = (Path(__file__).parent.parent / 'publisher' / 'mii_publisher.py').read_text(encoding='utf-8')
        self.assertNotIn('from src.', source)
        self.assertNotIn('sys.path.insert', source)

    def test_extract_net_liquidation(self):
        assets = [SimpleNamespace(summary=SimpleNamespace(net_liquidation='1234.5'))]
        self.assertEqual(extract_net_liquidation(assets), 1234.5)
        with self.assertRaises(ValueError):
            extract_net_liquidation([])

    def test_collect_account_uses_only_read_endpoints(self):
        trade = SimpleNamespace(
            get_positions=lambda: [position()],
            get_assets=lambda: [SimpleNamespace(summary=SimpleNamespace(net_liquidation=1000.0))],
        )
        positions, equity = collect_account(trade)
        self.assertEqual(len(positions), 1)
        self.assertEqual(equity, 1000.0)


class InstanceTests(unittest.TestCase):
    def config(self):
        return {
            'default_instance': 'test_data',
            'instances': [
                {'id': 'test_data', 'name': 'test_data', 'tiger_config': '/private/max', 'fills_file': '/private/max/fills', 'enabled': True},
                {'id': 'growth', 'name': 'Growth', 'tiger_config': '/private/growth', 'fills_file': '/private/growth/fills', 'enabled': True},
            ],
        }

    def test_instance_config_loads_and_selects_without_sharing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instances.json'
            path.write_text(json.dumps(self.config()), encoding='utf-8')
            config = load_instances(path)
        self.assertEqual([item['id'] for item in select_instances(config)], ['test_data', 'growth'])
        self.assertEqual([item['id'] for item in select_instances(config, 'growth')], ['growth'])
        with self.assertRaises(ValueError):
            select_instances(config, 'unknown')

    def test_invalid_or_duplicate_instance_ids_are_rejected(self):
        for instance_id in ('../secret', 'Max Account', 'account space', ''):
            config = self.config()
            config['instances'][0]['id'] = instance_id
            with self.subTest(instance_id=instance_id), self.assertRaises(ValueError):
                public_registry(config)
        config = self.config()
        config['instances'][1]['id'] = 'test_data'
        with self.assertRaises(ValueError):
            public_registry(config)

    def test_disabled_instance_is_not_publicly_registered(self):
        config = self.config()
        config['instances'][1]['enabled'] = False
        registry = public_registry(config)
        self.assertEqual([item['id'] for item in registry['instances']], ['test_data'])

    def test_public_registry_contains_only_instance_id_and_name(self):
        registry = public_registry(self.config())
        self.assertEqual(registry['default_instance'], 'test_data')
        self.assertEqual(registry['instances'][0], {'id': 'test_data', 'name': 'test_data'})
        text = json.dumps(registry)
        self.assertNotIn('tiger_config', text)
        self.assertNotIn('fills_file', text)
        self.assertNotIn('/private/', text)

    def test_fixture_instances_are_explicitly_marked_as_tests(self):
        config = self.config()
        config['instances'][1]['source'] = 'fixture'
        registry = public_registry(config)
        self.assertNotIn('test', registry['instances'][0])
        self.assertTrue(registry['instances'][1]['test'])

    def test_fixture_snapshot_advances_price_and_equity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            path.write_text(json.dumps({'snapshots': [
                {'equity': 20000, 'positions': [{'symbol': 'TESTA', 'quantity': 100, 'average_cost': 90, 'price': 100}]},
                {'equity': 21000, 'positions': [{'symbol': 'TESTA', 'quantity': 100, 'average_cost': 90, 'price': 105}]},
            ]}), encoding='utf-8')
            first_positions, first_equity, first_cursor = load_fixture_snapshot(path, 0)
            second_positions, second_equity, second_cursor = load_fixture_snapshot(path, first_cursor)
        self.assertEqual(first_equity, 20000)
        self.assertEqual(second_equity, 21000)
        self.assertEqual(second_cursor, 2)
        self.assertNotEqual(first_positions[0].market_price, second_positions[0].market_price)

    def test_fixture_instance_updates_twice_without_touching_max(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'private'
            repo = root / 'repo'
            fixture = root / 'fixture.json'
            fixture.write_text(json.dumps({'snapshots': [
                {'equity': 20000, 'positions': [{'symbol': 'TESTA', 'quantity': 100, 'average_cost': 90, 'price': 100}]},
                {'equity': 21000, 'positions': [{'symbol': 'TESTA', 'quantity': 100, 'average_cost': 90, 'price': 105}]},
            ]}), encoding='utf-8')
            fills = root / 'fills.jsonl'
            records = [
                {'record_type': 'buy_components', 'order_id': 'test-order'},
                {'record_type': 'fill', 'order_id': 'test-order', 'symbol': 'TESTA', 'price': 100, 'qty': 100, 'time': '2026-01-01T12:00:00'},
            ]
            fills.write_text('\n'.join(json.dumps(record) for record in records) + '\n', encoding='utf-8')
            repo.mkdir()
            subprocess.run(['git', 'init', '-b', 'main'], cwd=repo, check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'MII Test'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'mii@example.invalid'], cwd=repo, check=True)
            max_latest = repo / 'data' / 'instances' / 'test_data' / 'latest.json'
            max_latest.parent.mkdir(parents=True)
            max_latest.write_text('{"preserve": "test_data"}', encoding='utf-8')
            max_state = private / 'instances' / 'test_data' / 'private_state.json'
            max_state.parent.mkdir(parents=True)
            max_state.write_text('{"preserve": "max-state"}', encoding='utf-8')
            instance = {
                'id': 'fixture-account', 'name': '账号2（测试）', 'source': 'fixture',
                'fixture_file': str(fixture), 'fills_file': str(fills), 'enabled': True,
            }
            config = {
                'default_instance': 'test_data',
                'instances': [
                    {'id': 'test_data', 'name': 'test_data', 'tiger_config': '/max', 'fills_file': '/max/fills'},
                    instance,
                ],
            }
            args = SimpleNamespace(tiger_config=None, fills_file=None, dry_run=False, no_push=True, repo=str(repo))
            with patch('publisher.mii_publisher.PRIVATE_DIR', private):
                run_instance(instance, args, public_registry(config))
                first = json.loads((repo / 'data' / 'instances' / 'fixture-account' / 'latest.json').read_text(encoding='utf-8'))
                run_instance(instance, args, public_registry(config))
                second = json.loads((repo / 'data' / 'instances' / 'fixture-account' / 'latest.json').read_text(encoding='utf-8'))

            self.assertNotEqual(first['mii_index'], second['mii_index'])
            self.assertEqual(len(json.loads((repo / 'data' / 'instances' / 'fixture-account' / 'history.json').read_text(encoding='utf-8'))), 2)
            self.assertEqual(json.loads(max_latest.read_text(encoding='utf-8'))['preserve'], 'test_data')
            self.assertEqual(json.loads(max_state.read_text(encoding='utf-8'))['preserve'], 'max-state')
            state = json.loads((private / 'instances' / 'fixture-account' / 'private_state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['fixture_cursor'], 2)

class PublicFileTests(unittest.TestCase):
    def files(self):
        latest = {
            'schema_version': 1, 'generated_at': '2026-01-01T12:00:00+00:00',
            'mii_index': 100.0, 'total_return_pct': 0.0, 'day_return_pct': None,
            'holdings': [],
        }
        return {'latest': latest, 'history': [], 'events': []}

    def test_security_scan_accepts_public_dto_and_prices(self):
        files = self.files()
        files['latest']['holdings'] = [{
            'symbol': 'AAPL', 'average_cost': 90.0, 'current_price': 100.0,
        }]
        files['events'] = [{'symbol': 'AAPL', 'action': 'buy', 'price': 100.0}]
        assert_public_payload_safe(files)

    def test_security_scan_rejects_private_fields_and_tokens(self):
        for key in ('quantity', 'qty', 'equity_usd', 'account_id', 'order_id', 'market_price', 'market_value', 'units', 'nav'):
            files = self.files()
            files['latest'][key] = 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                assert_public_payload_safe(files)
        files = self.files()
        files['latest']['note'] = 'ghp_abcdefghijklmnopqrstuvwxyz123456'
        with self.assertRaises(ValueError):
            assert_public_payload_safe(files)

    def test_second_instance_does_not_overwrite_default_instance_data(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-b', 'main'], cwd=repo, check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'MII Test'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'mii@example.invalid'], cwd=repo, check=True)
            config = {
                'default_instance': 'test_data',
                'instances': [
                    {'id': 'test_data', 'name': 'test_data', 'tiger_config': '/max', 'fills_file': '/max/fills'},
                    {'id': 'growth', 'name': 'Growth', 'tiger_config': '/growth', 'fills_file': '/growth/fills'},
                ],
            }
            registry = public_registry(config)
            publish_files(repo, self.files(), push=False, instance_id='test_data', registry=registry)
            other = self.files()
            other['latest']['mii_index'] = 101.0
            publish_files(repo, other, push=False, instance_id='growth', registry=registry)

            self.assertFalse((repo / 'data' / 'latest.json').exists())
            self.assertEqual(json.loads((repo / 'data' / 'instances' / 'test_data' / 'latest.json').read_text(encoding='utf-8'))['mii_index'], 100.0)
            self.assertEqual(json.loads((repo / 'data' / 'instances' / 'growth' / 'latest.json').read_text(encoding='utf-8'))['mii_index'], 101.0)
            self.assertEqual(json.loads((repo / 'data' / 'instances.json').read_text(encoding='utf-8'))['default_instance'], 'test_data')

    def test_publish_files_commits_only_actual_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-b', 'main'], cwd=repo, check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.name', 'MII Test'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'mii@example.invalid'], cwd=repo, check=True)

            registry = public_registry({
                'default_instance': 'test_data',
                'instances': [{'id': 'test_data', 'name': 'test_data'}],
            })
            self.assertTrue(publish_files(repo, self.files(), push=False, instance_id='test_data', registry=registry))
            self.assertFalse(publish_files(repo, self.files(), push=False, instance_id='test_data', registry=registry))
            count = subprocess.check_output(
                ['git', 'rev-list', '--count', 'HEAD'], cwd=repo, text=True).strip()
            self.assertEqual(count, '1')
            self.assertTrue((repo / 'data' / 'instances' / 'test_data' / 'latest.json').is_file())
            self.assertFalse((repo / 'data' / 'latest.json').exists())
            subprocess.run(['git', 'status', '--porcelain'], cwd=repo, check=True)


if __name__ == '__main__':
    unittest.main()
