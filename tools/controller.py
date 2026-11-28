#!/usr/bin/env python3
"""TCP/JSON controller: protect selected nodes, store durably, then release."""
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from durable_json import atomic_json, ID
from tcp_json import MAX_JSON_BYTES, receive_frame, send_frame


MAX_CONTROL_BYTES = MAX_JSON_BYTES
MAX_CAPTURE_MICROSECONDS = 50_000_000
RPC_ATTEMPTS = 6
REPLAY_TTL_SECONDS = 300
REPLAY_MAX_ENTRIES = 1024
DEFAULT_ACQUISITION = 'tianlai'
COMPLETION_PORT = 19001


def normalize_node_ranges(reference_period, configurations, selected_ids,
                          period_microseconds, integration_per_trace):
    """Convert per-node second offsets and durations to exact period ranges."""
    if (type(reference_period) is not int or reference_period < 0 or
            reference_period >= (1 << 20) * integration_per_trace):
        raise ValueError('reference_period is outside the acquisition period range')
    if not isinstance(configurations, dict) or set(configurations) != {
            str(node_id) for node_id in selected_ids}:
        raise ValueError('node_ranges must specify every selected server ID')

    def exact_microseconds(value, name):
        if type(value) not in (int, float):
            raise ValueError(name + ' must be a JSON number')
        try:
            result = Decimal(str(value)) * 1000000
            if not result.is_finite() or result != result.to_integral_value():
                raise ValueError(name + ' must resolve to whole microseconds')
            return int(result)
        except InvalidOperation:
            raise ValueError(name + ' must be a finite number')

    result = {}
    epoch_end = (1 << 20) * integration_per_trace
    for node_id in selected_ids:
        settings = configurations[str(node_id)]
        if not isinstance(settings, dict):
            raise ValueError('each node range must be an object')
        if set(settings) == {'offset_seconds', 'duration_seconds'}:
            offset = exact_microseconds(settings['offset_seconds'], 'offset_seconds')
            duration = exact_microseconds(settings['duration_seconds'], 'duration_seconds')
        elif set(settings) == {'start_offset_seconds', 'end_offset_seconds'}:
            offset = exact_microseconds(settings['start_offset_seconds'], 'start_offset_seconds')
            end_offset = exact_microseconds(settings['end_offset_seconds'], 'end_offset_seconds')
            duration = end_offset - offset
        else:
            raise ValueError('use offset_seconds and duration_seconds, or start_offset_seconds and end_offset_seconds')
        if offset % period_microseconds or duration % period_microseconds:
            raise ValueError('node range must align with integration periods')
        if not period_microseconds <= duration <= MAX_CAPTURE_MICROSECONDS:
            raise ValueError('node duration must be between one integration period and 50 seconds')
        start = reference_period + offset // period_microseconds
        count = duration // period_microseconds
        if start < 0 or start + count > epoch_end:
            raise ValueError('node range exceeds the acquisition period range')
        if start + count > reference_period:
            raise ValueError('node range must end at or before reference_period')
        result[str(node_id)] = [start, count]
    return result


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


def manager_rpc(node, action):
    """Ask one root cache-manager service to perform a fixed systemd action."""
    request = dict(version=1, request_id=uuid.uuid4().hex, action=action)
    body = json.dumps(request, separators=(',', ':')).encode()
    peer = (socket.gethostbyname(node['host']), node.get('manager_port', 19002))
    connect_timeout = .5 if action == 'STATUS' else 3
    reply_timeout = 1 if action == 'STATUS' else 90
    try:
        with socket.create_connection(peer, timeout=connect_timeout) as connection:
            connection.settimeout(reply_timeout)
            send_frame(connection, body)
            reply = parse_request(receive_frame(connection))
        if reply.get('request_id') != request['request_id']:
            raise ValueError('cache manager request ID mismatch')
        return dict(server_id=node['server_id'], status=reply.get('status', 'ERROR'),
                    service_state=reply.get('service_state', 'unknown'),
                    error=str(reply.get('error', ''))[:80])
    except Exception as error:
        return dict(server_id=node['server_id'], status='ERROR',
                    service_state='unknown', error=str(error)[:80])


class ReplayCache:
    """Return the same reply for a retried backend request with the same request ID."""
    def __init__(self, capacity=REPLAY_MAX_ENTRIES, ttl=REPLAY_TTL_SECONDS):
        self.capacity = capacity
        self.ttl = ttl
        self.entries = OrderedDict()
        self.entries_lock = threading.Lock()
        self.request_locks = [threading.Lock() for _ in range(64)]
        self.mutation_lock = threading.Lock()

    def get(self, peer, request_id, digest):
        key = (peer[0], request_id)
        with self.entries_lock:
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
        with self.entries_lock:
            self.entries[key] = (digest, time.monotonic() + self.ttl, reply)
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)


def backend_reply(controller, data, peer, cache):
    """Validate one backend request and provide replay-safe bounded JSON."""
    try:
        message = parse_request(data)
        request_id = message['request_id'] if 'request_id' in message else uuid.uuid4().hex
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ValueError('invalid request_id')
    except (KeyError, ValueError, TypeError, UnicodeDecodeError):
        return _backend_reply(controller, data, peer, cache)
    command = message.get('command')
    read_only = command in ('QUERY_STATUS', 'QUERY_EVENT', 'QUERY_ACTIVE_EVENT', 'QUERY_STORAGE') or (
        command == 'CACHE_SERVICE' and message.get('action') == 'STATUS')
    # Requests with the same identity cannot execute concurrently. Mutations
    # also remain serialized against service operations and event submission.
    request_lock = cache.request_locks[hash((peer[0], request_id)) % len(cache.request_locks)]
    with request_lock:
        with nullcontext() if read_only else cache.mutation_lock:
            return _backend_reply(controller, data, peer, cache, request_id)


def _backend_reply(controller, data, peer, cache, generated_request_id=None):
    request_id = ''
    cacheable = True
    digest = hashlib.sha256(data).digest()
    try:
        message = parse_request(data)
        request_id = message['request_id'] if 'request_id' in message else (
            generated_request_id or uuid.uuid4().hex)
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ValueError('invalid request_id')
        cached = cache.get(peer, request_id, digest)
        if cached is not None:
            return cached
        # Generate the event ID only after the replay lookup. A caller that
        # supplies a request ID can retry the identical request safely while
        # the replay entry is retained, even when event_id was omitted.
        if message.get('command') == 'CAPTURE' and 'event_id' not in message:
            message['event_id'] = 'event-' + uuid.uuid4().hex
        reply = controller.dispatch(message, peer)
    except Exception as error:
        if str(error) == 'REQUEST_ID_CONFLICT':
            cacheable = False
        reply = dict(status='ERROR', error=str(error)[:350])
    if 'node_ranges' in reply:
        # Shared-range fields describe only the first node internally.
        for field in ('start', 'end_exclusive', 'period_count'):
            reply.pop(field, None)
    reply.update(version=1, request_id=request_id)
    body = json.dumps(reply, separators=(',', ':')).encode()
    if (len(body) > MAX_CONTROL_BYTES and reply.get('action') in ('START', 'STOP', 'RESTART')
            and isinstance(reply.get('nodes'), dict)):
        # Preserve every node's outcome when verbose error messages exceed the
        # protocol limit; a failed operation must not appear unexecuted.
        compact = dict(reply, nodes={key: {k: v for k, v in value.items() if k != 'error'}
                                     for key, value in reply['nodes'].items()},
                       errors_omitted=True)
        body = json.dumps(compact, separators=(',', ':')).encode()
    if len(body) > MAX_CONTROL_BYTES and 'node_ranges' in reply:
        reply.pop('node_ranges')
        reply['node_ranges_omitted'] = True
        body = json.dumps(reply, separators=(',', ':')).encode()
    if len(body) > MAX_CONTROL_BYTES:
        body = json.dumps(dict(version=1, request_id=request_id, status='ERROR',
                               error='REPLY_TOO_LARGE'), separators=(',', ':')).encode()
    if cacheable and isinstance(request_id, str) and ID.fullmatch(request_id):
        cache.put(peer, request_id, digest, body)
    return body


class Controller:
    def __init__(self, nodes, acquisition, journal, allow_partial=False,
                 integration_per_trace=50000, period_microseconds=200,
                 storage_status='/run/tianlai-storage-status.json', admin_sources=()):
        def valid_node(node):
            return (isinstance(node, dict) and type(node.get('server_id')) is int and
                    0 <= node['server_id'] <= 13 and isinstance(node.get('host'), str) and
                    0 < len(node['host']) <= 255 and
                    type(node.get('port', 19000)) is int and
                    1024 <= node.get('port', 19000) <= 65535 and
                    type(node.get('manager_port', 19002)) is int and
                    1024 <= node.get('manager_port', 19002) <= 65535 and
                    type(node.get('enabled', True)) is bool)
        if (not isinstance(nodes, list) or not nodes or not all(map(valid_node, nodes)) or
                len({n['server_id'] for n in nodes}) != len(nodes) or
                len({(n['host'], n.get('port', 19000)) for n in nodes}) != len(nodes) or
                len({(n['host'], n.get('manager_port', 19002)) for n in nodes}) != len(nodes) or
                not ID.fullmatch(acquisition)):
            raise ValueError('invalid node list/acquisition ID')
        configured_nodes = sorted(nodes, key=lambda n: n['server_id'])
        self.nodes = [node for node in configured_nodes if node.get('enabled', True)]
        self.all_nodes = list(configured_nodes)
        if allow_partial:
            configured_ids = {node['server_id'] for node in configured_nodes}
            self.all_nodes.extend(
                dict(server_id=server_id, host=f'172.0.0.{128 + server_id}',
                     port=19000, manager_port=19002, enabled=False)
                for server_id in range(14) if server_id not in configured_ids)
            self.all_nodes.sort(key=lambda node: node['server_id'])
        self.admin_sources = (set(admin_sources) |
                              {node['host'] for node in self.all_nodes} |
                              {'172.0.0.1', '172.0.0.142'})
        self.allow_partial = allow_partial
        if not allow_partial and {n['server_id'] for n in self.nodes} != set(range(14)):
            raise ValueError('production requires all 14 nodes; use allow_partial only for testing')
        if (type(integration_per_trace) is not int or not 1 <= integration_per_trace <= (1 << 23) or
                type(period_microseconds) is not int or not 1 <= period_microseconds <= 1000000 or
                1000000 % period_microseconds):
            raise ValueError('invalid acquisition timing')
        self.integration_per_trace = integration_per_trace
        self.period_microseconds = period_microseconds
        self.acquisition = acquisition
        self.journal = Path(journal)
        self.storage_status = Path(storage_status)
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

    def query_storage(self):
        """Read the colocated storage receiver status without exposing its filesystem."""
        try:
            status = json.loads(self.storage_status.read_text(encoding='utf-8'))
            if not isinstance(status, dict) or status.get('service') != 'tianlai-storage':
                raise ValueError('invalid storage status')
            pid = status.get('pid')
            if type(pid) is not int or pid <= 0:
                raise ValueError('invalid storage PID')
            updated = status.get('updated_unix')
            if type(updated) is not int or abs(time.time() - updated) > 10:
                raise ValueError('stale storage status')
            try:
                os.kill(pid, 0)
            except PermissionError:
                pass
            return status
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return dict(service='tianlai-storage', state='OFFLINE', error=str(error),
                        listeners_ready=0, listener_count=0, active_transfers=0,
                        event_id='', event_expected_lanes=0, event_completed_lanes=0,
                        event_expected_bytes=0,
                        event_aborted_lanes=0, event_written_bytes=0,
                        completed_lanes_total=0, aborted_lanes_total=0,
                        written_bytes_total=0, write_rate_bytes_per_second=0)

    def update(self, event_id, **fields):
        """Persist the new event state before publishing it to other threads."""
        with self.lock:
            event = self.events[event_id]
            updated = dict(event, **fields)
            atomic_json(self.journal / f'{event_id}.json', updated)
            event.update(updated)

    def event_nodes(self, channel_count, server_ids=None):
        """Return the requested available nodes allowed by the channel mode."""
        if type(channel_count) is not int or channel_count not in (288, 384):
            raise ValueError('channel_count must be 288 or 384')
        expected = set(range(11 if channel_count == 288 else 14))
        requested = expected
        if server_ids is not None:
            if (not isinstance(server_ids, list) or not server_ids or
                    any(type(value) is not int for value in server_ids) or
                    len(set(server_ids)) != len(server_ids) or not set(server_ids) <= expected):
                raise ValueError('server_ids must be a unique non-empty list allowed by channel_count')
            requested = set(server_ids)
        selected = [node for node in self.nodes if node['server_id'] in requested]
        if not selected or ({node['server_id'] for node in selected} != requested and not self.allow_partial):
            raise ValueError(f'{channel_count}-channel node set is incomplete')
        return selected

    def stored_event_nodes(self, event):
        """Restore the exact participant set persisted with an event."""
        return self.event_nodes(event.get('channel_count', 384),
                                event.get('participating_server_ids'))

    def manage_cache_services(self, message, peer):
        """Fan out a fixed service action to selected cache-manager agents."""
        if peer is None or peer[0] not in self.admin_sources:
            raise PermissionError('cache service control is not allowed from this source')
        action = message.get('action')
        server_ids = message.get('server_ids')
        force = message.get('force', False)
        if (action not in ('START', 'STOP', 'RESTART', 'STATUS') or
                not isinstance(server_ids, list) or not server_ids or
                any(type(value) is not int for value in server_ids) or
                len(set(server_ids)) != len(server_ids) or type(force) is not bool):
            raise ValueError('invalid cache service request')
        by_id = {node['server_id']: node for node in self.all_nodes}
        if not set(server_ids) <= set(by_id):
            raise ValueError('unknown cache server ID')
        if action in ('STOP', 'RESTART') and not force:
            selected = set(server_ids)
            with self.lock:
                active = []
                for event in self.events.values():
                    participants = event.get('participating_server_ids')
                    if participants is None:
                        participants = range(11 if event.get('channel_count', 384) == 288 else 14)
                    if (event.get('status') not in ('COMPLETED', 'CANCELLED') and
                            selected.intersection(participants)):
                        active.append(event)
            if active:
                names = ','.join(event['event_id'] for event in active)
                raise RuntimeError(f'active events prevent cache service operation: {names}')
        nodes = [by_id[server_id] for server_id in sorted(server_ids)]
        with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
            results = list(workers.map(lambda node: manager_rpc(node, action), nodes))
        node_states = {}
        for result in results:
            state = dict(status=result['status'], state=result['service_state'])
            if action != 'STATUS' and result.get('error'):
                state['error'] = result['error'][:60]
            node_states[str(result['server_id'])] = state
        return dict(status='OK', action=action, nodes=node_states)

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

    def node_status(self, node):
        """Return cache runtime state even when the cache process is intentionally stopped."""
        try:
            return rpc(node, self.acquisition, 'QUERY_STATUS', timeout=.15, retries=1)
        except Exception as cache_error:
            service = manager_rpc(node, 'STATUS')
            service_state = service.get('service_state', 'unknown')
            if service.get('status') != 'OK':
                runtime_state = 'OFFLINE'
                error = service.get('error') or str(cache_error)
            elif service_state == 'active':
                runtime_state = 'STARTING'
                error = str(cache_error)
            else:
                runtime_state = 'STOPPED'
                error = ''
            result = dict(server_id=node['server_id'], status=runtime_state,
                          runtime_state=runtime_state, service_state=service_state,
                          accept_trigger=False, oldest_period=0, settled_end=0)
            if error:
                result['error'] = str(error)[:120]
            return result

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
        if 'start_period' in message or 'end_period' in message:
            if any(field in message for field in ('start', 'period_count', 'duration_ms')):
                raise ValueError('absolute endpoints cannot be combined with start or duration')
            first, end = message.get('start_period'), message.get('end_period')
            if type(first) is not int or type(end) is not int or not 0 <= first < end:
                raise ValueError('provide integer periods with 0 <= start_period < end_period')
            trace, integration = divmod(first, self.integration_per_trace)
            return self.capture_range(dict(
                start=dict(trace_period_id=trace, integration_period_id=integration),
                period_count=end - first))
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
            if (type(duration) is not int or duration <= 0 or
                    duration * 1000 % self.period_microseconds):
                raise ValueError('duration_ms must be a positive integer aligned to integration periods')
            count = duration * 1000 // self.period_microseconds
        else:
            count = message['period_count']
        if (type(count) is not int or count < 1 or
                count * self.period_microseconds > MAX_CAPTURE_MICROSECONDS or
                trace * self.integration_per_trace + integration + count > (1 << 20) * self.integration_per_trace):
            raise ValueError('invalid trigger range')
        end = trace * self.integration_per_trace + integration + count
        return (dict(trace_period_id=trace, integration_period_id=integration), count,
                dict(trace_period_id=end // self.integration_per_trace,
                     integration_period_id=end % self.integration_per_trace))

    def submit(self, message, resume=False, peer=None):
        """Deduplicate by event ID, persist the job, and start background scheduling."""
        event_id = message['event_id']
        if not ID.fullmatch(event_id):
            raise ValueError('invalid event_id')
        channel_count = None if resume else 384
        selected_nodes = None
        node_ranges = None
        if not resume:
            requested_ids = message.get('server_ids')
            if 'node_ranges' in message:
                if not isinstance(message['node_ranges'], dict):
                    raise ValueError('node_ranges must be an object')
                if requested_ids is None:
                    try:
                        requested_ids = [int(key) for key in message['node_ranges']]
                    except (TypeError, ValueError):
                        raise ValueError('node_ranges keys must be server IDs')
            if requested_ids:
                if not isinstance(requested_ids, list) or any(type(node_id) is not int for node_id in requested_ids):
                    raise ValueError('server IDs must be integers')
                channel_count = 384 if any(node_id >= 11 for node_id in requested_ids) else 288
            selected_nodes = self.event_nodes(channel_count, requested_ids)
            if 'node_ranges' in message:
                if any(key in message for key in ('start', 'duration_ms', 'period_count',
                                                  'start_period', 'end_period')):
                    raise ValueError('node_ranges cannot be combined with a shared range')
                node_ranges = normalize_node_ranges(
                    message.get('reference_period'), message['node_ranges'],
                    [node['server_id'] for node in selected_nodes],
                    self.period_microseconds, self.integration_per_trace)
                first_start, first_count = node_ranges[str(selected_nodes[0]['server_id'])]
                start_obj = dict(trace_period_id=first_start // self.integration_per_trace,
                                 integration_period_id=first_start % self.integration_per_trace)
                end = first_start + first_count
                normalized = (start_obj, first_count,
                              dict(trace_period_id=end // self.integration_per_trace,
                                   integration_period_id=end % self.integration_per_trace))
            else:
                normalized = self.capture_range(message)
        if not resume and ('result_address' in message or 'result_port' in message):
            raise ValueError('completion endpoint is assigned automatically')
        notify = False if resume else message.get('notify', peer is not None)
        if type(notify) is not bool:
            raise ValueError('notify must be a boolean')
        if notify and peer is None:
            raise ValueError('completion notification requires a TCP peer')
        result_port = COMPLETION_PORT if notify else 0
        result_address = peer[0] if notify else ''
        with self.lock:
            existing = self.events.get(event_id)
            if existing and not resume:
                if (existing.get('node_ranges') != node_ranges or
                        existing['start'] != normalized[0] or existing['period_count'] != normalized[1] or
                        existing.get('channel_count', 384) != channel_count or
                        existing.get('result_address', '') != result_address or
                        existing.get('result_port', 0) != result_port or
                        existing.get('participating_server_ids') !=
                        [node['server_id'] for node in selected_nodes]):
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
                    result_address=result_address, result_port=result_port,
                    partial_node_set=len(participant_ids) != expected_count,
                    participating_server_ids=participant_ids)
                if node_ranges is not None:
                    self.events[event_id]['node_ranges'] = node_ranges
                    self.events[event_id]['reference_period'] = message['reference_period']
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
            nodes = self.stored_event_nodes(event)
            server_mask = sum(1 << node['server_id'] for node in nodes)
            if 'node_ranges' in event and event['status'] == 'PREPARING':
                with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                    snapshots = list(workers.map(self.node_status, nodes))
                for node, snapshot in zip(nodes, snapshots):
                    first, count = event['node_ranges'][str(node['server_id'])]
                    if (not snapshot.get('accept_trigger') or
                            first < snapshot.get('oldest_period', 0) or
                            first + count > snapshot.get('settled_end', 0)):
                        raise ValueError(f"server {node['server_id']}: range not in settled cache")
            def prepare(node):
                if 'node_ranges' in event:
                    absolute, count = event['node_ranges'][str(node['server_id'])]
                    start = dict(trace_period_id=absolute // self.integration_per_trace,
                                 integration_period_id=absolute % self.integration_per_trace)
                else:
                    start, count = event['start'], event['period_count']
                return rpc(node, self.acquisition, 'PREPARE', event_id=event_id,
                           start=start, period_count=count,
                           channel_count=event.get('channel_count', 384),
                           server_mask=server_mask,
                           result_address=event.get('result_address', ''),
                           result_port=event.get('result_port', 0))
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                states = list(workers.map(prepare, nodes))
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

    def dispatch(self, message, peer=None):
        """Handle backend trigger, query, recovery, and cancellation requests."""
        if type(message.get('version')) is not int or message['version'] != 1:
            raise ValueError('version/acquisition mismatch')
        if 'channel_count' in message:
            raise ValueError('channel_count is no longer accepted; use server_ids')
        if message.get('acquisition_id', self.acquisition) != self.acquisition:
            raise ValueError('version/acquisition mismatch')
        if 'acquisition_id' not in message:
            message = dict(message, acquisition_id=self.acquisition)
        command = message['command']
        if command == 'CACHE_SERVICE':
            return self.manage_cache_services(message, peer)
        if command == 'QUERY_STORAGE':
            return dict(status='READY', storage=self.query_storage())
        if command == 'CAPTURE':
            return self.submit(message, peer=peer)
        if command == 'RESUME':
            return self.submit(message, resume=True, peer=peer)
        if command == 'QUERY_EVENT':
            with self.lock:
                return dict(self.events[message['event_id']])
        if command == 'QUERY_ACTIVE_EVENT':
            with self.lock:
                if self.running is not None and self.running in self.events:
                    return dict(self.events[self.running])
                active = [event for event in self.events.values()
                          if event.get('status') not in ('COMPLETED', 'CANCELLED')]
                if len(active) == 1:
                    return dict(active[0])
                if not active:
                    raise ValueError('NO_ACTIVE_EVENT')
                raise RuntimeError('MULTIPLE_ACTIVE_EVENTS')
        if command == 'CANCEL':
            event_id = message['event_id']
            with self.lock:
                if self.running is not None:
                    raise RuntimeError('BUSY: wait for active operation before cancelling')
                if event_id not in self.events:
                    raise ValueError('EVENT_NOT_FOUND')
            # Resolve terminal states first; never cancel a node still sending.
            event = self.events[event_id]
            for node in self.stored_event_nodes(event):
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
            requested_ids = message.get('server_ids')
            if requested_ids and (not isinstance(requested_ids, list) or
                                  any(type(node_id) is not int for node_id in requested_ids)):
                raise ValueError('server IDs must be integers')
            channel_count = (288 if requested_ids and all(node_id < 11 for node_id in requested_ids)
                             else 384)
            nodes = self.event_nodes(channel_count, requested_ids)
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                states = list(workers.map(self.node_status, nodes))
            oldest = max(s.get('oldest_period', 0) for s in states)
            end = min(s.get('settled_end', 0) for s in states)
            ready = not busy and oldest < end and all(s.get('accept_trigger', False) for s in states)
            node_runtime_states = {
                str(node['server_id']): state.get('runtime_state', state.get('status', 'UNKNOWN'))
                for node, state in zip(nodes, states)
            }
            runtime_state_counts = {}
            for state in node_runtime_states.values():
                runtime_state_counts[state] = runtime_state_counts.get(state, 0) + 1
            summary = ('READY' if ready else 'STOPPED'
                       if set(node_runtime_states.values()) == {'STOPPED'} else 'BUSY_OR_WARMING')
            return dict(status=summary, accept_trigger=ready,
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
    # Kept as an ignored compatibility option so an older systemd unit still
    # starts while a rolling software update replaces the unit file.
    parser.add_argument('--allow-source', help=argparse.SUPPRESS)
    parser.add_argument('--allow-partial', action='store_true', help='allow fewer than 14 nodes for testing only')
    parser.add_argument('--integration-per-trace', type=int, default=50000)
    parser.add_argument('--period-us', type=int, default=200)
    parser.add_argument('--admin-source', action='append', default=[],
                        help='management IPv4 allowed to start or stop cache services')
    args = parser.parse_args()
    controller = Controller(json.loads(Path(args.nodes).read_text()), args.acquisition, args.journal,
                            allow_partial=args.allow_partial, integration_per_trace=args.integration_per_trace,
                            period_microseconds=args.period_us, admin_sources=args.admin_source)
    replay = ReplayCache()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.bind, args.port))
        sock.listen(16)
        print(f'TCP/JSON trigger controller listening on {sock.getsockname()}, '
              'accepting every reachable source', flush=True)
        def handle_connection(connection, peer):
            with connection:
                connection.settimeout(5)
                try:
                    send_frame(connection, backend_reply(
                        controller, receive_frame(connection), peer, replay))
                except (ConnectionError, OSError, ValueError):
                    pass

        with ThreadPoolExecutor(max_workers=32) as workers:
            while True:
                connection, peer = sock.accept()
                workers.submit(handle_connection, connection, peer)


if __name__ == '__main__':
    main()
