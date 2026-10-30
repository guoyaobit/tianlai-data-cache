#!/usr/bin/env python3
"""Model 14 nodes to verify the prepare barrier and overlapping exports."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from controller import Controller


class Nodes:
    def __init__(self, fail_remote=False, fail_export=False):
        self.lock = threading.Lock()
        self.states = {i: 'IDLE' for i in range(14)}
        self.protected = set()
        self.sent = []
        self.fail_remote = fail_remote
        self.pending_remote = True
        self.fail_export = fail_export

    def rpc(self, node, acquisition, command, **fields):
        i = node['server_id']
        assert acquisition == 'shared-acquisition'
        with self.lock:
            if command == 'PREPARE':
                assert fields['start'] == {'trace_period_id': 120, 'integration_period_id': 45000}
                assert fields['period_count'] == 15000
                if i == 13 and self.fail_remote:
                    raise TimeoutError('remote node 13 unreachable')
                if self.states[i] == 'IDLE':
                    self.states[i] = 'PREPARING' if i == 13 else 'PREPARED'
                    if i != 13:
                        self.protected.add(i)
            elif command == 'QUERY_EVENT':
                if i == 13 and self.states[i] == 'PREPARING':
                    if self.pending_remote:
                        self.pending_remote = False
                    else:
                        self.states[i] = 'PREPARED'
                        self.protected.add(i)
                if self.states[i] == 'SENDING' and len(set(self.sent)) == 14:
                    if i == 7 and self.fail_export:
                        self.states[i] = 'SEND_FAILED'
                        self.fail_export = False
                    else:
                        self.states[i] = 'STORED'
            elif command == 'COMMIT':
                assert self.protected == set(range(14)), 'committed before all nodes protected'
                assert self.states[i] == 'PREPARED'
                self.states[i] = 'QUEUED'
            elif command == 'SEND':
                assert self.protected == set(range(14)), 'sent before remote nodes protected'
                if i not in self.sent:
                    assert not any(s == 'RELEASED' for s in self.states.values()), 'waited before authorizing all senders'
                assert self.states[i] in ('QUEUED', 'SEND_FAILED')
                self.sent.append(i)
                self.states[i] = 'SENDING'
            elif command == 'RELEASE':
                assert self.states[i] == 'STORED'
                self.states[i] = 'RELEASED'
            else:
                raise AssertionError(command)
            return {'server_id': i, 'status': self.states[i]}


def finish(controller):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with controller.lock:
            if controller.running is None:
                return dict(controller.events['all-14'])
        time.sleep(.01)
    raise AssertionError('controller did not finish')


def main():
    config = json.loads((ROOT / 'tools/nodes_example.json').read_text())
    assert [n['server_id'] for n in config] == list(range(14))
    message = dict(version=1, acquisition_id='shared-acquisition', command='CAPTURE', event_id='all-14',
                   start=dict(trace_period_id=120, integration_period_id=45000), period_count=15000)
    for failure in (False, True):
        with tempfile.TemporaryDirectory(prefix='tianlai-controller-') as path:
            model = Nodes(fail_remote=failure)
            with patch('controller.rpc', model.rpc):
                controller = Controller(config, 'shared-acquisition', path)
                controller.dispatch(message)
                event = finish(controller)
                if failure:
                    assert event['status'] == 'PAUSED' and not model.sent, event
                    assert model.protected == set(range(13))
                    model.fail_remote = False
                    controller = Controller(config, 'shared-acquisition', path)
                    controller.dispatch(dict(message, command='RESUME'))
                    event = finish(controller)
                assert event['status'] == 'COMPLETED', event
                assert sorted(model.sent) == list(range(14))
                assert controller.dispatch(message)['status'] == 'COMPLETED'
    with tempfile.TemporaryDirectory(prefix='tianlai-controller-send-failure-') as path:
        model = Nodes(fail_export=True)
        with patch('controller.rpc', model.rpc):
            controller = Controller(config, 'shared-acquisition', path)
            controller.dispatch(message)
            event = finish(controller)
            assert event['status'] == 'PAUSED' and model.states[7] == 'SEND_FAILED', event
            assert all(model.states[i] == 'RELEASED' for i in range(14) if i != 7)
            controller = Controller(config, 'shared-acquisition', path)
            controller.dispatch(dict(message, command='RESUME'))
            assert finish(controller)['status'] == 'COMPLETED'
            assert len(model.sent) == 15 and model.sent.count(7) == 2
    print('14-node shared trigger: prepare barrier, concurrent export, prepare/send failure and restart resume passed')


if __name__ == '__main__':
    main()
