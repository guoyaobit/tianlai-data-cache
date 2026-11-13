#!/usr/bin/env python3
"""Validate shared history, input bounds, and safe publication on journal failure."""
import sys
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import controller
import trigger


def main():
    assert trigger.normalize_arguments([]) == ['status']
    assert trigger.normalize_arguments(['3']) == ['capture', '3', '--wait']
    assert trigger.normalize_arguments(['3', '--wait']) == ['capture', '3', '--wait']
    assert trigger.normalize_arguments(['event-example']) == ['event', 'event-example']
    simple = trigger.create_parser().parse_args(['capture', '3', '--channels', '288'])
    with patch('trigger.transmit', return_value=dict(
            status='READY', accept_trigger=True, oldest_period=100000,
            settled_end=120000)):
        generated = trigger.request_from_arguments(simple)
    assert generated['command'] == 'CAPTURE' and generated['channel_count'] == 288
    assert generated['start'] == dict(trace_period_id=2, integration_period_id=5000)
    assert generated['duration_ms'] == 3000 and generated['event_id'].startswith('event-')
    maximum = trigger.create_parser().parse_args(['capture', '50', '--channels', '288'])
    with patch('trigger.transmit', return_value=dict(
            status='READY', accept_trigger=True, oldest_period=0,
            settled_end=300000)):
        maximum_request = trigger.request_from_arguments(maximum)
    assert maximum_request['duration_ms'] == 50000

    nodes = [dict(server_id=0, host='127.0.0.1', port=19000),
             dict(server_id=1, host='127.0.0.1', port=19001)]
    with tempfile.TemporaryDirectory() as directory, patch('durable_json.sync_directory'):
        storage_status = Path(directory) / 'storage-status.json'
        storage_status.write_text(json.dumps(dict(
            version=1, service='tianlai-storage', pid=os.getpid(),
            state='WAITING_FOR_DATA', listeners_ready=28, listener_count=28)))
        c = controller.Controller(nodes, 'acq', directory, allow_partial=True,
                                  storage_status=storage_status)
        storage = c.dispatch(dict(version=1, command='QUERY_STORAGE'))['storage']
        assert storage['state'] == 'WAITING_FOR_DATA' and storage['listeners_ready'] == 28
        try:
            c.dispatch(dict(version=1, acquisition_id='wrong', command='QUERY_STATUS'))
            raise AssertionError('wrong acquisition identifier accepted')
        except ValueError:
            pass
        start = dict(trace_period_id=120, integration_period_id=45000)
        by_duration = c.capture_range(dict(start=start, duration_ms=3000))
        assert by_duration == c.capture_range(dict(start=start, period_count=15000))
        assert by_duration[2] == dict(trace_period_id=121, integration_period_id=10000)
        maximum_range = c.capture_range(dict(start=start, duration_ms=50000))
        assert maximum_range[1] == 250000
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
            without_acquisition = dict(base, duration_ms=3000)
            del without_acquisition['acquisition_id']
            assert replay.dispatch(without_acquisition) == first
            second = replay.dispatch(dict(base, period_count=15000))
            assert first == second and first['end_exclusive'] == by_duration[2]
            try:
                replay.dispatch(dict(base, period_count=15001))
                raise AssertionError('changed event range accepted')
            except ValueError:
                pass
            try:
                replay.dispatch(dict(base, period_count=15000, channel_count=288))
                raise AssertionError('changed channel count accepted for an existing event')
            except ValueError:
                pass
        for invalid in (dict(start=start, duration_ms=0), dict(start=start, duration_ms=50001),
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
        for channels in (0, 287, 385, True, '288'):
            request = dict(version=1, acquisition_id='acq', command='CAPTURE', event_id='bad-mode',
                           start=start, period_count=1, channel_count=channels)
            try:
                c.dispatch(request)
                raise AssertionError('invalid channel count accepted')
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
            status = {'PREPARE': 'PREPARED', 'STORE': 'STORED',
                      'QUERY_EVENT': 'STORED', 'RELEASE': 'RELEASED'}[command]
            return dict(status=status, server_id=node['server_id'], missing_packets=0)
        with patch('controller.rpc', fake_rpc), patch('controller.atomic_json', failing_journal):
            c.run_event('event')
        assert c.events['event']['status'] == 'PAUSED'
        assert 'journal failure' in c.events['event']['error'] and c.running is None

        class Backend:
            def __init__(self):
                self.calls = 0
            def dispatch(self, message):
                self.calls += 1
                return {'status': 'READY', 'call': self.calls}
        backend = Backend()
        cache = controller.ReplayCache(capacity=4, ttl=60)
        request = json.dumps(dict(version=1, request_id='request-1', acquisition_id='acq',
                                  command='QUERY_STATUS')).encode()
        request_without_acquisition = json.dumps(dict(
            version=1, request_id='request-2', command='QUERY_STATUS')).encode()
        first = controller.backend_reply(backend, request, ('127.0.0.1', 1000), cache)
        second = controller.backend_reply(backend, request, ('127.0.0.1', 2000), cache)
        assert first == second and backend.calls == 1
        conflict = json.dumps(dict(version=1, request_id='request-1', acquisition_id='acq',
                                   command='QUERY_EVENT', event_id='event')).encode()
        rejected = json.loads(controller.backend_reply(
            backend, conflict, ('127.0.0.1', 3000), cache))
        assert rejected['status'] == 'ERROR' and rejected['error'] == 'REQUEST_ID_CONFLICT'
        assert controller.backend_reply(backend, request, ('127.0.0.1', 4000), cache) == first
        assert backend.calls == 1
        assert json.loads(controller.backend_reply(
            backend, request_without_acquisition, ('127.0.0.1', 5000), cache))['status'] == 'READY'
        assert backend.calls == 2
    print('shared-range, replay safety, trigger bounds, and journal failure checks passed')


if __name__ == '__main__':
    main()
