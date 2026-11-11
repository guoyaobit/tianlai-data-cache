#!/usr/bin/env python3
"""TCP/JSON controller: protect selected nodes, store durably, then release."""
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from durable_json import atomic_json, ID
from tcp_json import MAX_JSON_BYTES, receive_frame, send_frame


MAX_CONTROL_BYTES = MAX_JSON_BYTES
RPC_ATTEMPTS = 6
REPLAY_TTL_SECONDS = 300
REPLAY_MAX_ENTRIES = 1024
DEFAULT_ACQUISITION = 'tianlai'


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


def rpc(node, acquisition, command, *, timeout=.25, retries=RPC_ATTEMPTS, **fields):
    """Call one node with a stable request ID and bounded exponential retries."""
    request = dict(version=1, request_id=uuid.uuid4().hex, acquisition_id=acquisition,
                   command=command, **fields)
    body = json.dumps(request, separators=(',', ':')).encode()
    if len(body) > MAX_CONTROL_BYTES:
        raise ValueError('control message too long')
    peer = (socket.gethostbyname(node['host']), node.get('port', 19000))
    for attempt in range(retries):
        attempt_timeout = timeout * min(8, 1 << attempt)
        try:
            with socket.create_connection(peer, timeout=attempt_timeout) as sock:
                sock.settimeout(attempt_timeout)
                send_frame(sock, body)
                reply = parse_request(receive_frame(sock))
            if (reply.get('version') == 1 and
                    reply.get('request_id') == request['request_id'] and
                    reply.get('server_id') == node['server_id']):
                if reply.get('status') == 'ERROR':
                    raise RuntimeError(f"server {node['server_id']}: {reply.get('error')}")
                return reply
        except (ConnectionError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
    raise TimeoutError(f"server {node['server_id']}: TCP request timeout")


class ReplayCache:
    """Return the same reply for a retried backend request with the same request ID."""
    def __init__(self, capacity=REPLAY_MAX_ENTRIES, ttl=REPLAY_TTL_SECONDS):
        self.capacity = capacity
        self.ttl = ttl
        self.entries = OrderedDict()

    def get(self, peer, request_id, digest):
        key = (peer[0], request_id)
        entry = self.entries.get(key)
        if entry is None:
            return None
        saved_digest, expires, reply = entry
        if expires <= time.monotonic():
            del self.entries[key]
            return None
        if saved_digest != digest:
            raise ValueError('REQUEST_ID_CONFLICT')
        self.entries.move_to_end(key)
        return reply

    def put(self, peer, request_id, digest, reply):
        key = (peer[0], request_id)
        self.entries[key] = (digest, time.monotonic() + self.ttl, reply)
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)


def backend_reply(controller, data, peer, cache):
    """Validate one backend request and provide replay-safe bounded JSON."""
    request_id = ''
    cacheable = True
    digest = hashlib.sha256(data).digest()
    try:
        message = parse_request(data)
        request_id = message['request_id']
        if not ID.fullmatch(request_id):
            raise ValueError('invalid request_id')
        cached = cache.get(peer, request_id, digest)
        if cached is not None:
            return cached
        reply = controller.dispatch(message)
    except Exception as error:
        if str(error) == 'REQUEST_ID_CONFLICT':
            cacheable = False
        reply = dict(status='ERROR', error=str(error)[:350])
    reply.update(version=1, request_id=request_id)
    body = json.dumps(reply, separators=(',', ':')).encode()
    if len(body) > MAX_CONTROL_BYTES:
        body = json.dumps(dict(version=1, request_id=request_id, status='ERROR',
                               error='REPLY_TOO_LARGE'), separators=(',', ':')).encode()
    if cacheable and ID.fullmatch(request_id):
        cache.put(peer, request_id, digest, body)
    return body


class Controller:
    def __init__(self, nodes, acquisition, journal, allow_partial=False,
                 integration_per_trace=50000, period_microseconds=200):
        def valid_node(node):
            return (isinstance(node, dict) and type(node.get('server_id')) is int and
                    0 <= node['server_id'] <= 13 and isinstance(node.get('host'), str) and
                    0 < len(node['host']) <= 255 and
                    type(node.get('port', 19000)) is int and
                    1024 <= node.get('port', 19000) <= 65535)
        if (not isinstance(nodes, list) or not nodes or not all(map(valid_node, nodes)) or
                len({n['server_id'] for n in nodes}) != len(nodes) or
                len({(n['host'], n.get('port', 19000)) for n in nodes}) != len(nodes) or
                not ID.fullmatch(acquisition)):
            raise ValueError('invalid node list/acquisition ID')
        self.nodes = sorted(nodes, key=lambda n: n['server_id'])
        self.allow_partial = allow_partial
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
        """Run one idempotent command on all selected nodes and report every failure."""
        selected = self.nodes if nodes is None else nodes
        with ThreadPoolExecutor(max_workers=len(selected)) as workers:
            futures = [workers.submit(rpc, n, self.acquisition, command, **fields) for n in selected]
            results = []
            errors = []
            for node, future in zip(selected, futures):
                try:
                    results.append(future.result())
                except Exception as caught:
                    errors.append(f"server {node['server_id']}: {caught}")
            if errors:
                raise RuntimeError('; '.join(errors))
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
        selected_nodes = None
        if not resume:
            selected_nodes = self.event_nodes(channel_count)
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
                participant_ids = [node['server_id'] for node in selected_nodes]
                expected_count = 11 if channel_count == 288 else 14
                self.events[event_id] = dict(event_id=event_id, acquisition_id=self.acquisition,
                    start=start, end_exclusive=end, period_count=count,
                    channel_count=channel_count,
                    period_microseconds=self.period_microseconds, integration_per_trace=self.integration_per_trace,
                    status='PREPARING', active_server_ids=[],
                    partial_node_set=len(participant_ids) != expected_count,
                    participating_server_ids=participant_ids)
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
        """Protect all nodes, store all data, then release only after the global durable barrier."""
        try:
            event = self.events[event_id]
            nodes = self.event_nodes(event.get('channel_count', 384))
            states = self.parallel('PREPARE', nodes=nodes, event_id=event_id,
                                   start=event['start'], period_count=event['period_count'],
                                   channel_count=event.get('channel_count', 384))
            missing_by_server = dict(event.get('missing_by_server', {}))
            for index, (node, state) in enumerate(zip(nodes, states)):
                if state['status'] == 'PREPARING':
                    state = self.poll_event(node, event_id, {'PREPARED'}, 120)
                    states[index] = state
                if state['status'] not in ('PREPARED', 'SENDING', 'SEND_FAILED', 'STORED', 'RELEASED'):
                    raise RuntimeError(f"server {node['server_id']}: {state}")
                if 'missing_packets' in state:
                    missing_by_server[str(node['server_id'])] = state['missing_packets']
            self.update(event_id, status='PREPARED', error='',
                        missing_by_server=missing_by_server,
                        active_server_ids=[node['server_id'] for node in nodes])

            # Persist the phase before issuing the side effect. STORE is idempotent,
            # so a restart can safely reconcile every node from its actual state.
            self.update(event_id, status='STORING')
            to_start = [node for node, state in zip(nodes, states)
                        if state['status'] in ('PREPARED', 'SEND_FAILED')]
            if to_start:
                self.parallel('STORE', nodes=to_start, event_id=event_id)

            def wait_stored(node):
                state = rpc(node, self.acquisition, 'QUERY_EVENT', event_id=event_id)
                if state['status'] == 'SENDING':
                    state = self.poll_event(node, event_id, {'STORED', 'RELEASED'}, 3600)
                if state['status'] not in ('STORED', 'RELEASED'):
                    raise RuntimeError(f"server {node['server_id']}: {state}")
                return state

            # No node is released until every selected node has durable storage.
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                futures = [workers.submit(wait_stored, node) for node in nodes]
                stored, errors = [], []
                for node, future in zip(nodes, futures):
                    try:
                        stored.append(future.result())
                    except Exception as error:
                        errors.append(f"server {node['server_id']}: {error}")
                if errors:
                    raise RuntimeError('; '.join(errors))
            for node, state in zip(nodes, stored):
                if 'missing_packets' in state:
                    missing_by_server[str(node['server_id'])] = state['missing_packets']
            self.update(event_id, status='STORED', missing_by_server=missing_by_server)

            self.update(event_id, status='RELEASING')
            to_release = [node for node, state in zip(nodes, stored)
                          if state['status'] == 'STORED']
            if to_release:
                released = self.parallel('RELEASE', nodes=to_release, event_id=event_id)
                if any(state['status'] != 'RELEASED' for state in released):
                    raise RuntimeError('one or more nodes did not release the stored event')
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
        if type(message.get('version')) is not int or message['version'] != 1:
            raise ValueError('version/acquisition mismatch')
        if message.get('acquisition_id', self.acquisition) != self.acquisition:
            raise ValueError('version/acquisition mismatch')
        if 'acquisition_id' not in message:
            message = dict(message, acquisition_id=self.acquisition)
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
            node_runtime_states = {
                str(node['server_id']): state.get('runtime_state', state.get('status', 'UNKNOWN'))
                for node, state in zip(nodes, states)
            }
            runtime_state_counts = {}
            for state in node_runtime_states.values():
                runtime_state_counts[state] = runtime_state_counts.get(state, 0) + 1
            return dict(status='READY' if ready else 'BUSY_OR_WARMING', accept_trigger=ready,
                        channel_count=channel_count, oldest_period=oldest, settled_end=end,
                        partial_node_set=len(nodes) != (11 if channel_count == 288 else 14),
                        participating_server_ids=[node['server_id'] for node in nodes],
                        node_runtime_states=node_runtime_states,
                        runtime_state_counts=runtime_state_counts)
        raise ValueError('unknown controller command')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', required=True, help='JSON node list')
    parser.add_argument('--acquisition', default=DEFAULT_ACQUISITION,
                        help='internal identifier; external requests may omit it')
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
    replay = ReplayCache()
    allowed = socket.gethostbyname(args.allow_source)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.bind, args.port))
        sock.listen(16)
        print(f'TCP/JSON trigger controller listening on {sock.getsockname()}', flush=True)
        while True:
            connection, peer = sock.accept()
            with connection:
                if peer[0] != allowed:
                    continue
                connection.settimeout(5)
                try:
                    send_frame(connection, backend_reply(
                        controller, receive_frame(connection), peer, replay))
                except (ConnectionError, OSError, ValueError):
                    continue


if __name__ == '__main__':
    main()
