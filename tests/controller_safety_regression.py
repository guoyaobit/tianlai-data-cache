#!/usr/bin/env python3
"""Validate shared history, input bounds, and safe publication on journal failure."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import controller


def main():
    nodes = [dict(server_id=0, host='127.0.0.1'), dict(server_id=1, host='127.0.0.1')]
    with tempfile.TemporaryDirectory() as directory, patch('durable_json.sync_directory'):
        c = controller.Controller(nodes, 'acq', directory, allow_partial=True)
        start = dict(trace_period_id=120, integration_period_id=45000)
        by_duration = c.capture_range(dict(start=start, duration_ms=3000))
        assert by_duration == c.capture_range(dict(start=start, period_count=15000))
        assert by_duration[2] == dict(trace_period_id=121, integration_period_id=10000)
        with tempfile.TemporaryDirectory() as another:
            alternate = controller.Controller(nodes, 'acq', another, allow_partial=True, period_microseconds=800)
            try:
                alternate.capture_range(dict(start=start, duration_ms=1))
                raise AssertionError('nonintegral period duration accepted')
            except ValueError:
                pass
        with tempfile.TemporaryDirectory() as another, patch('controller.threading.Thread.start'):
            replay = controller.Controller(nodes, 'acq', another, allow_partial=True)
            base = dict(version=1, acquisition_id='acq', command='CAPTURE', event_id='same', start=start)
            first = replay.dispatch(dict(base, duration_ms=3000))
            second = replay.dispatch(dict(base, period_count=15000))
            assert first == second and first['end_exclusive'] == by_duration[2]
            try:
                replay.dispatch(dict(base, period_count=15001))
                raise AssertionError('changed event range accepted')
            except ValueError:
                pass
        for invalid in (dict(start=start, duration_ms=0), dict(start=start, duration_ms=5001),
                        dict(start=start, duration_ms=1, period_count=5), dict(start=start),
                        dict(start=start, duration_ms=True), dict(start=start, period_count=1.0),
                        dict(start=dict(trace_period_id=0, integration_period_id=50000), period_count=1)):
            try:
                c.capture_range(invalid)
                raise AssertionError('invalid trigger accepted')
            except ValueError:
                pass
        for payload in (b'{"event_id":"a","event_id":"b"}', b'[]', b'{"duration_ms":NaN}'):
            try:
                controller.parse_request(payload)
                raise AssertionError('invalid JSON accepted')
            except ValueError:
                pass
        with patch.object(c, 'parallel', return_value=[
                dict(accept_trigger=True, oldest_period=0, settled_end=100),
                dict(accept_trigger=True, oldest_period=100, settled_end=200)]):
            assert not c.dispatch(dict(version=1, acquisition_id='acq', command='QUERY_STATUS'))['accept_trigger']
        with patch.object(c, 'parallel', return_value=[
                dict(accept_trigger=True, oldest_period=0, settled_end=101),
                dict(accept_trigger=True, oldest_period=100, settled_end=200)]):
            assert c.dispatch(dict(version=1, acquisition_id='acq', command='QUERY_STATUS'))['accept_trigger']
        for trace, integration in [(-1, 0), (1 << 20, 0), (0, -1), (0, 1 << 23)]:
            req = dict(version=1, acquisition_id='acq', command='CAPTURE', event_id='invalid',
                       start=dict(trace_period_id=trace, integration_period_id=integration), period_count=1)
            try:
                c.dispatch(req)
                raise AssertionError('invalid packet-header counter accepted')
            except ValueError:
                pass
            assert 'invalid' not in c.events

        c.events['event'] = dict(event_id='event', acquisition_id='acq', status='PREPARING',
            start=dict(trace_period_id=1, integration_period_id=0), period_count=1)
        c.running = 'event'
        real_atomic = controller.atomic_json
        def failing_journal(path, event):
            if event['status'] in ('COMPLETED', 'PAUSED'):
                raise OSError('disk full')
            return real_atomic(path, event)
        def fake_rpc(node, acquisition, command, **fields):
            status = {'PREPARE': 'PREPARED', 'COMMIT': 'QUEUED', 'QUERY_EVENT': 'QUEUED',
                      'SEND': 'STORED', 'RELEASE': 'RELEASED'}[command]
            return dict(status=status, server_id=node['server_id'], missing_packets=0)
        with patch('controller.rpc', fake_rpc), patch('controller.atomic_json', failing_journal):
            c.run_event('event')
        assert c.events['event']['status'] == 'PAUSED'
        assert 'journal failure' in c.events['event']['error'] and c.running is None
    print('shared-range, trigger bounds, and journal failure checks passed')


if __name__ == '__main__':
    main()
