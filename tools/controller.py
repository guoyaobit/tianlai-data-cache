#!/usr/bin/env python3
"""UDP controller: protect every node selected by the channel mode, then export concurrently."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from durable_json import atomic_json, ID


def parse_request(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field: ' + key)
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('invalid JSON constant: ' + value)
    message = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(message, dict):
        raise ValueError('JSON object required')
    return message


def rpc(node, acquisition, command, *, timeout=.2, retries=5, **fields):
    """Send a control request to a node and return the reply after matching the request and node IDs."""
    request = dict(version=1, request_id=uuid.uuid4().hex, acquisition_id=acquisition,
                   command=command, **fields)
    body = json.dumps(request, separators=(',', ':')).encode()
    if len(body) > 1200:
        raise ValueError('control message too long')
    peer = (socket.gethostbyname(node['host']), node.get('port', 19000))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(retries):
            sock.sendto(body, peer)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    data, address = sock.recvfrom(1201)
                except socket.timeout:
                    break
                if address != peer or len(data) > 1200:
                    continue
                reply = json.loads(data)
                if reply.get('request_id') == request['request_id'] and reply.get('server_id') == node['server_id']:
                    if reply.get('status') == 'ERROR':
                        raise RuntimeError(f"server {node['server_id']}: {reply.get('error')}")
                    return reply
    raise TimeoutError(f"server {node['server_id']}: UDP acknowledgment timeout")


class Controller:
    def __init__(self, nodes, acquisition, journal, allow_partial=False,
                 integration_per_trace=50000, period_microseconds=200):
        self.nodes = sorted(nodes, key=lambda n: n['server_id'])
        self.allow_partial = allow_partial
        if (len({n['server_id'] for n in nodes}) != len(nodes) or not nodes or
                any(not 0 <= n['server_id'] <= 13 for n in nodes) or not ID.fullmatch(acquisition)):
            raise ValueError('invalid node list/acquisition ID')
        if not allow_partial and {n['server_id'] for n in nodes} != set(range(14)):
            raise ValueError('production requires all 14 nodes; use allow_partial only for testing')
        if (type(integration_per_trace) is not int or not 1 <= integration_per_trace <= (1 << 23) or
                type(period_microseconds) is not int or not 1 <= period_microseconds <= 1000000 or
                1000000 % period_microseconds):
            raise ValueError('invalid acquisition timing')
        self.integration_per_trace = integration_per_trace
        self.period_microseconds = period_microseconds
        self.acquisition = acquisition
        self.journal = Path(journal)
        self.journal.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.events = {}
        self.running = None
        for path in self.journal.glob('*.json'):
            record = json.loads(path.read_text())
            if record.get('acquisition_id') == acquisition:
                record.setdefault('channel_count', 384)
                if (record.get('integration_per_trace', integration_per_trace) != integration_per_trace or
                        record.get('period_microseconds', period_microseconds) != period_microseconds):
                    raise ValueError('journal acquisition timing mismatch')
                if record['status'] not in ('COMPLETED', 'CANCELLED'):
                    record['status'] = 'PAUSED'
                self.events[record['event_id']] = record

    def update(self, event_id, **fields):
        """Persist the new event state before publishing it to other threads."""
        with self.lock:
            event = self.events[event_id]
            updated = dict(event, **fields)
            atomic_json(self.journal / f'{event_id}.json', updated)
            event.update(updated)

    def event_nodes(self, channel_count):
        """Return the complete node set for a 288- or 384-channel event."""
        if type(channel_count) is not int or channel_count not in (288, 384):
            raise ValueError('channel_count must be 288 or 384')
        expected = set(range(11 if channel_count == 288 else 14))
        selected = [node for node in self.nodes if node['server_id'] in expected]
        if not selected or ({node['server_id'] for node in selected} != expected and not self.allow_partial):
            raise ValueError(f'{channel_count}-channel node set is incomplete')
        return selected

    def parallel(self, command, nodes=None, **fields):
        selected = self.nodes if nodes is None else nodes
        with ThreadPoolExecutor(max_workers=len(selected)) as workers:
            futures = [workers.submit(rpc, n, self.acquisition, command, **fields) for n in selected]
            results = []
            error = None
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as caught:
                    error = caught
            if error:
                raise error
            return results

    def poll_event(self, node, event_id, terminal, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = rpc(node, self.acquisition, 'QUERY_EVENT', event_id=event_id)
            if state['status'] in terminal:
                return state
            if state['status'] in ('REJECTED', 'CANCELLED', 'SEND_FAILED'):
                raise RuntimeError(f"server {node['server_id']}: {state}")
            time.sleep(.2)
        raise TimeoutError(f"server {node['server_id']}: operation still pending; buffers remain protected")

    def capture_range(self, message):
        """Validate the trigger start and length, then return the period count and exclusive end position."""
        start = message['start']
        if not isinstance(start, dict):
            raise ValueError('start must be an object')
        trace, integration = start.get('trace_period_id'), start.get('integration_period_id')
        if (type(trace) is not int or not 0 <= trace < (1 << 20) or
                type(integration) is not int or not 0 <= integration < self.integration_per_trace):
            raise ValueError('invalid trigger start')
        if ('period_count' in message) == ('duration_ms' in message):
            raise ValueError('provide exactly one of period_count or duration_ms')
        if 'duration_ms' in message:
            duration = message['duration_ms']
            if type(duration) is not int or duration <= 0 or duration * 1000 % self.period_microseconds:
                raise ValueError('duration_ms must be a positive whole number of periods')
            count = duration * 1000 // self.period_microseconds
        else:
            count = message['period_count']
        if (type(count) is not int or count < 1 or count * self.period_microseconds > 5000000 or
                trace * self.integration_per_trace + integration + count > (1 << 20) * self.integration_per_trace):
            raise ValueError('invalid trigger range')
        end = trace * self.integration_per_trace + integration + count
        return (dict(trace_period_id=trace, integration_period_id=integration), count,
                dict(trace_period_id=end // self.integration_per_trace,
                     integration_period_id=end % self.integration_per_trace))

    def submit(self, message, resume=False):
        """Deduplicate by event ID, persist the job, and start background scheduling."""
        event_id = message['event_id']
        if not ID.fullmatch(event_id):
            raise ValueError('invalid event_id')
        channel_count = None if resume else message.get('channel_count', 384)
        if not resume:
            self.event_nodes(channel_count)
        normalized = None if resume else self.capture_range(message)
        with self.lock:
            existing = self.events.get(event_id)
            if existing and not resume:
                if (existing['start'] != normalized[0] or existing['period_count'] != normalized[1] or
                        existing.get('channel_count', 384) != channel_count):
                    raise ValueError('EVENT_ID_CONFLICT')
                return dict(existing)
            if self.running is not None or any(e['status'] not in ('COMPLETED', 'CANCELLED')
                    for key, e in self.events.items() if key != event_id):
                raise RuntimeError('BUSY: resolve the previous event before accepting another')
            if resume:
                if existing is None or existing['status'] != 'PAUSED':
                    raise ValueError('only PAUSED events may resume')
            else:
                start, count, end = normalized
                self.events[event_id] = dict(event_id=event_id, acquisition_id=self.acquisition,
                    start=start, end_exclusive=end, period_count=count,
                    channel_count=channel_count,
                    period_microseconds=self.period_microseconds, integration_per_trace=self.integration_per_trace,
                    status='PREPARING', active_server_ids=[])
            self.running = event_id
            try:
                atomic_json(self.journal / f'{event_id}.json', self.events[event_id])
                threading.Thread(target=self.run_event, args=(event_id,), daemon=True).start()
            except Exception:
                self.running = None
                self.events[event_id]['status'] = 'PAUSED'
                raise
            return dict(self.events[event_id])

    def run_event(self, event_id):
        """Wait for all nodes to become ready, send in parallel, then confirm persistence and release each node."""
        try:
            event = self.events[event_id]
            nodes = self.event_nodes(event.get('channel_count', 384))
            # PREPARE is idempotent. Reconciliation also works after controller restart.
            prepared = self.parallel('PREPARE', nodes=nodes, event_id=event_id, start=event['start'],
                                     period_count=event['period_count'],
                                     channel_count=event.get('channel_count', 384))
            missing_by_server = dict(event.get('missing_by_server', {}))
            needs_commit = []
            for node, state in zip(nodes, prepared):
                if state['status'] == 'PREPARING':
                    state = self.poll_event(node, event_id, {'PREPARED'}, 120)
                if state['status'] not in ('PREPARED', 'QUEUED', 'SENDING', 'SEND_FAILED', 'STORED', 'RELEASED'):
                    raise RuntimeError(f"server {node['server_id']}: {state}")
                if 'missing_packets' in state:
                    missing_by_server[str(node['server_id'])] = state['missing_packets']
                if state['status'] == 'PREPARED':
                    needs_commit.append(node)
            self.update(event_id, missing_by_server=missing_by_server)
            self.update(event_id, status='ACCEPTED', error='')
            # The prepare barrier is complete. Commit only nodes that still need it,
            # in parallel; a partial failure remains recoverable via PREPARE/RESUME.
            if needs_commit:
                with ThreadPoolExecutor(max_workers=len(needs_commit)) as workers:
                    futures = [workers.submit(rpc, node, self.acquisition, 'COMMIT', event_id=event_id)
                               for node in needs_commit]
                    for future in futures:
                        future.result()
            self.update(event_id, status='SENDING', active_server_ids=[n['server_id'] for n in nodes])
            # Authorize every selected node before waiting for durable completion.
            # Per-node rate limits are not redistributed when fewer nodes participate.
            def export_node(node):
                state = rpc(node, self.acquisition, 'QUERY_EVENT', event_id=event_id)
                if state['status'] in ('QUEUED', 'SEND_FAILED'):
                    state = rpc(node, self.acquisition, 'SEND', event_id=event_id)
                if state['status'] == 'SENDING':
                    state = self.poll_event(node, event_id, {'STORED'}, 3600)
                if state['status'] == 'STORED':
                    state = rpc(node, self.acquisition, 'RELEASE', event_id=event_id)
                if state['status'] != 'RELEASED':
                    raise RuntimeError(f"unexpected node state: {state}")
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                futures = [workers.submit(export_node, node) for node in nodes]
                errors = []
                for future in futures:
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(str(error))
                if errors:
                    raise RuntimeError('; '.join(errors))
            self.update(event_id, status='COMPLETED', active_server_ids=[])
        except Exception as error:
            # Do not free protected data after an ambiguous UDP timeout or storage failure.
            try:
                self.update(event_id, status='PAUSED', error=str(error)[:350])
            except Exception as journal_error:
                # Never leave an in-memory COMPLETED status visible if journaling failed.
                # The journal may be stale; restart requires operator reconciliation.
                with self.lock:
                    self.events[event_id].update(status='PAUSED',
                        error=('journal failure: ' + str(journal_error))[:350])
        finally:
            with self.lock:
                self.running = None

    def dispatch(self, message):
        """Handle backend trigger, query, recovery, and cancellation requests."""
        if type(message.get('version')) is not int or message['version'] != 1 or message.get('acquisition_id') != self.acquisition:
            raise ValueError('version/acquisition mismatch')
        command = message['command']
        if command == 'CAPTURE':
            return self.submit(message)
        if command == 'RESUME':
            return self.submit(message, resume=True)
        if command == 'QUERY_EVENT':
            with self.lock:
                return dict(self.events[message['event_id']])
        if command == 'CANCEL':
            event_id = message['event_id']
            with self.lock:
                if self.running is not None:
                    raise RuntimeError('BUSY: wait for active operation before cancelling')
                if event_id not in self.events:
                    raise ValueError('EVENT_NOT_FOUND')
            # Resolve terminal states first; never cancel a node still sending.
            event = self.events[event_id]
            for node in self.event_nodes(event.get('channel_count', 384)):
                try:
                    state = rpc(node, self.acquisition, 'QUERY_EVENT', event_id=event_id)
                except RuntimeError as error:
                    if 'EVENT_NOT_FOUND' not in str(error):
                        raise
                    state = {'status': 'UNKNOWN'}
                if state['status'] == 'STORED':
                    rpc(node, self.acquisition, 'RELEASE', event_id=event_id)
                elif state['status'] not in ('RELEASED', 'CANCELLED', 'REJECTED'):
                    result = rpc(node, self.acquisition, 'CANCEL', event_id=event_id)
                    if result['status'] == 'PREPARING':
                        self.poll_event(node, event_id, {'CANCELLED', 'REJECTED'}, 120)
            self.update(event_id, status='CANCELLED', active_server_ids=[])
            return dict(self.events[event_id])
        if command == 'QUERY_STATUS':
            with self.lock:
                busy = any(e['status'] not in ('COMPLETED', 'CANCELLED') for e in self.events.values())
            channel_count = message.get('channel_count', 384)
            nodes = self.event_nodes(channel_count)
            states = self.parallel('QUERY_STATUS', nodes=nodes)
            oldest = max(s['oldest_period'] for s in states)
            end = min(s['settled_end'] for s in states)
            ready = not busy and oldest < end and all(s['accept_trigger'] for s in states)
            return dict(status='READY' if ready else 'BUSY_OR_WARMING', accept_trigger=ready,
                        channel_count=channel_count, oldest_period=oldest, settled_end=end)
        raise ValueError('unknown controller command')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', required=True, help='JSON node list')
    parser.add_argument('--acquisition', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=18999)
    parser.add_argument('--allow-source', required=True)
    parser.add_argument('--allow-partial', action='store_true', help='allow fewer than 14 nodes for testing only')
    parser.add_argument('--integration-per-trace', type=int, default=50000)
    parser.add_argument('--period-us', type=int, default=200)
    args = parser.parse_args()
    controller = Controller(json.loads(Path(args.nodes).read_text()), args.acquisition, args.journal,
                            allow_partial=args.allow_partial, integration_per_trace=args.integration_per_trace,
                            period_microseconds=args.period_us)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((args.bind, args.port))
        print(f'UDP trigger controller listening on {sock.getsockname()}', flush=True)
        while True:
            data, peer = sock.recvfrom(1201)
            if peer[0] != args.allow_source or len(data) > 1200:
                continue
            request_id = ''
            try:
                message = parse_request(data)
                request_id = message['request_id']
                if not ID.fullmatch(request_id):
                    raise ValueError('invalid request_id')
                reply = controller.dispatch(message)
            except Exception as error:
                reply = dict(status='ERROR', error=str(error)[:350])
            reply.update(version=1, request_id=request_id)
            body = json.dumps(reply, separators=(',', ':')).encode()
            if len(body) <= 1200:
                sock.sendto(body, peer)


if __name__ == '__main__':
    main()
