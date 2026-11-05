#!/usr/bin/env python3
"""Controller scope and production node-list protection."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import controller


def main():
    nodes = json.loads((ROOT / 'tools/nodes_example.json').read_text())
    # Windows cannot fsync a directory; the production implementation still does.
    with tempfile.TemporaryDirectory() as directory, patch('durable_json.sync_directory'):
        try:
            controller.Controller(nodes[:-1], 'acq', directory)
            raise AssertionError('incomplete production node list accepted')
        except ValueError:
            pass
        for invalid_nodes in (
                [dict(server_id=True, host='127.0.0.1')],
                [dict(server_id=0, host='127.0.0.1'),
                 dict(server_id=1, host='127.0.0.1')],
                [dict(server_id=0, host='', port=19000)]):
            try:
                controller.Controller(invalid_nodes, 'acq', directory, allow_partial=True)
                raise AssertionError('invalid or duplicate node endpoint accepted')
            except ValueError:
                pass
        c = controller.Controller(nodes, 'acq', directory)
        c.events['event-1'] = dict(event_id='event-1', acquisition_id='acq', status='COMPLETED',
                                    start={'trace_period_id': 1, 'integration_period_id': 0},
                                    period_count=15000, missing_by_server={'0': 2})
        controller.atomic_json(Path(directory) / 'event-1.json', c.events['event-1'])
        # Completion is now sent directly by the C++ storage service. The trigger
        # controller no longer accepts backend notification acknowledgments.
        try:
            c.dispatch(dict(version=1, acquisition_id='acq', command='ACK_RESULT', event_id='event-1'))
            raise AssertionError('controller accepted storage completion acknowledgment')
        except ValueError as error:
            assert 'unknown controller command' in str(error)
        c = controller.Controller(nodes, 'acq', directory)
        request = dict(version=1, acquisition_id='acq', command='CAPTURE', event_id='event-2',
                       start={'trace_period_id': 1, 'integration_period_id': 0}, period_count=1)
        with patch('controller.atomic_json', side_effect=OSError('disk full')):
            try:
                c.dispatch(request)
                raise AssertionError('journal failure reported success')
            except OSError:
                pass
        assert c.running is None and c.events['event-2']['status'] == 'PAUSED'
    print('controller scope, journal failure and node validation passed')


if __name__ == '__main__':
    main()
