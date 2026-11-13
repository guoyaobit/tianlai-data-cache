#!/usr/bin/env python3
"""Simple Tianlai cache trigger client with JSON-file compatibility."""

import argparse
from datetime import datetime, timezone
import json
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

MAX_JSON_BYTES = 1200
MAX_CAPTURE_SECONDS = 50
LENGTH = struct.Struct('!I')
WAIT_SUCCESS = {'COMPLETED'}
WAIT_FAILURE = {'PAUSED', 'CANCELLED', 'REJECTED'}


def normalize_arguments(arguments):
    """Support short forms: `tltrigger 3` and `tltrigger event-ID`."""
    if not arguments:
        return ['status']
    if arguments[0].isdigit():
        normalized = ['capture'] + arguments
        if '--wait' not in normalized:
            normalized.append('--wait')
        return normalized
    if arguments[0].startswith('event-'):
        return ['event'] + arguments
    return arguments


def parse_servers(value):
    """Convert labels such as 1,2,5-7 or tianlaistore1 into zero-based IDs."""
    selected = set()
    for item in value.split(','):
        item = item.strip().lower()
        if item.startswith('tianlaistore'):
            item = item[len('tianlaistore'):]
        if '-' in item:
            first, last = item.split('-', 1)
            if not first.isdigit() or not last.isdigit() or int(first) > int(last):
                raise argparse.ArgumentTypeError('servers must use labels such as 1,2,5-7')
            labels = range(int(first), int(last) + 1)
        elif item.isdigit():
            labels = (int(item),)
        else:
            raise argparse.ArgumentTypeError('servers must use labels such as 1,2,5-7')
        for label in labels:
            if not 1 <= label <= 14:
                raise argparse.ArgumentTypeError('server labels must be between 1 and 14')
            selected.add(label - 1)
    if not selected:
        raise argparse.ArgumentTypeError('at least one server is required')
    return sorted(selected)


def receive_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError('TCP connection closed before the frame completed')
        data.extend(chunk)
    return bytes(data)


def receive_frame(connection):
    size = LENGTH.unpack(receive_exact(connection, LENGTH.size))[0]
    if not 1 <= size <= MAX_JSON_BYTES:
        raise ValueError('invalid TCP JSON frame length')
    return receive_exact(connection, size)


def send_frame(connection, body):
    if not 1 <= len(body) <= MAX_JSON_BYTES:
        raise ValueError('invalid TCP JSON frame length')
    connection.sendall(LENGTH.pack(len(body)) + body)


def create_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='172.0.0.142')
    parser.add_argument('--port', type=int, default=18999)
    parser.add_argument('--file', help='send an existing JSON request')
    parser.add_argument('--timeout', type=float, default=2)
    parser.add_argument('--retries', type=int, default=5)
    actions = parser.add_subparsers(dest='action')

    status = actions.add_parser('status', help='query trigger readiness')
    status.add_argument('--channels', type=int, choices=(288, 384), default=384)
    status.add_argument('--servers', type=parse_servers,
                        help='cache servers, for example 1,2,5-7 (default: all)')

    capture = actions.add_parser('capture', help='save the latest settled data')
    capture.add_argument('seconds', type=int, help='duration in seconds (1-50)')
    capture.add_argument('--channels', type=int, choices=(288, 384), default=384)
    capture.add_argument('--event-id', help='unique event ID; generated when omitted')
    capture.add_argument('--trace', type=int, help='optional explicit start trace')
    capture.add_argument('--integration', type=int, help='optional explicit start integration')
    capture.add_argument('--period-us', type=int, default=200, help=argparse.SUPPRESS)
    capture.add_argument('--integration-per-trace', type=int, default=50000,
                         help=argparse.SUPPRESS)
    capture.add_argument('--servers', type=parse_servers,
                         help='cache servers, for example 1,2,5-7 (default: all)')
    capture.add_argument('--wait', action='store_true',
                         help='wait until the event completes or needs attention')
    capture.add_argument('--interval', type=float, default=2,
                         help='event polling interval in seconds (default: 2)')
    capture.add_argument('--wait-timeout', type=float, default=7200,
                         help='maximum wait time in seconds (default: 7200)')

    for name, help_text in (('event', 'query one event'), ('resume', 'resume a paused event'),
                            ('cancel', 'cancel a paused event'),
                            ('wait', 'wait for one event to finish')):
        subcommand = actions.add_parser(name, help=help_text)
        subcommand.add_argument('event_id')
        if name == 'wait':
            subcommand.add_argument('--interval', type=float, default=2)
            subcommand.add_argument('--wait-timeout', type=float, default=7200)
    return parser


def transmit(request, arguments):
    request = dict(request)
    request.setdefault('version', 1)
    request.setdefault('request_id', uuid.uuid4().hex)
    body = json.dumps(request, separators=(',', ':')).encode()
    if len(body) > MAX_JSON_BYTES:
        raise ValueError('control message exceeds 1200 bytes')
    peer = (socket.gethostbyname(arguments.host), arguments.port)
    for attempt in range(arguments.retries):
        timeout = arguments.timeout * min(4, 1 << attempt)
        try:
            with socket.create_connection(peer, timeout=timeout) as connection:
                connection.settimeout(timeout)
                send_frame(connection, body)
                reply = json.loads(receive_frame(connection))
            if reply.get('request_id') == request['request_id']:
                return reply
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
            continue
    raise TimeoutError('no matching TCP reply; query the event before assuming failure')


def generated_event_id():
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return f'event-{timestamp}-{uuid.uuid4().hex[:6]}'


def capture_request(arguments):
    if not 1 <= arguments.seconds <= MAX_CAPTURE_SECONDS:
        raise ValueError(f'seconds must be between 1 and {MAX_CAPTURE_SECONDS}')
    if (arguments.trace is None) != (arguments.integration is None):
        raise ValueError('--trace and --integration must be used together')
    if (arguments.period_us <= 0 or 1000000 % arguments.period_us or
            not 1 <= arguments.integration_per_trace <= (1 << 23)):
        raise ValueError('invalid acquisition timing')

    if arguments.trace is None:
        status_request = dict(command='QUERY_STATUS', channel_count=arguments.channels)
        if arguments.servers is not None:
            status_request['server_ids'] = arguments.servers
        status = transmit(status_request, arguments)
        if status.get('status') != 'READY' or not status.get('accept_trigger'):
            print(json.dumps(status, indent=2, ensure_ascii=False))
            raise RuntimeError('cache is not ready for a trigger')
        oldest, settled_end = status.get('oldest_period'), status.get('settled_end')
        period_count = arguments.seconds * 1000000 // arguments.period_us
        if type(oldest) is not int or type(settled_end) is not int:
            raise RuntimeError('controller did not return a usable period range')
        start_absolute = settled_end - period_count
        if start_absolute < oldest:
            raise RuntimeError('the common cache range is shorter than the requested duration')
        trace, integration = divmod(start_absolute, arguments.integration_per_trace)
    else:
        trace, integration = arguments.trace, arguments.integration

    event_id = arguments.event_id or generated_event_id()
    labels = 'all' if arguments.servers is None else ','.join(str(value + 1) for value in arguments.servers)
    print(f'event={event_id} start={trace}:{integration} duration={arguments.seconds}s '
          f'channels={arguments.channels} servers={labels}', file=sys.stderr)
    request = dict(command='CAPTURE', event_id=event_id, channel_count=arguments.channels,
                   start=dict(trace_period_id=trace, integration_period_id=integration),
                   duration_ms=arguments.seconds * 1000)
    if arguments.servers is not None:
        request['server_ids'] = arguments.servers
    return request


def request_from_arguments(arguments):
    if arguments.file:
        if arguments.action:
            raise ValueError('--file cannot be combined with a command')
        return json.loads(Path(arguments.file).read_text(encoding='utf-8'))
    if arguments.action == 'status':
        request = dict(command='QUERY_STATUS', channel_count=arguments.channels)
        if arguments.servers is not None:
            request['server_ids'] = arguments.servers
        return request
    if arguments.action == 'capture':
        return capture_request(arguments)
    if arguments.action in ('event', 'resume', 'cancel', 'wait'):
        commands = dict(event='QUERY_EVENT', resume='RESUME', cancel='CANCEL')
        return dict(command=commands.get(arguments.action, 'QUERY_EVENT'),
                    event_id=arguments.event_id)
    raise ValueError('choose a duration, status, event, wait, resume or cancel')


def wait_for_event(event_id, arguments, initial=None):
    """Poll one event and return its terminal or operator-action state."""
    if arguments.interval <= 0 or arguments.wait_timeout <= 0:
        raise ValueError('wait interval and timeout must be positive')
    deadline = time.monotonic() + arguments.wait_timeout
    reply = initial
    previous = None
    while True:
        if reply is None:
            reply = transmit(dict(command='QUERY_EVENT', event_id=event_id), arguments)
        status = reply.get('status')
        if status != previous:
            print(f'event={event_id} status={status}', file=sys.stderr)
            previous = status
        if status in WAIT_SUCCESS or status in WAIT_FAILURE or status == 'ERROR':
            return reply
        if time.monotonic() >= deadline:
            raise TimeoutError(f'event {event_id} did not finish before the wait timeout')
        time.sleep(arguments.interval)
        reply = None


def main():
    arguments = create_parser().parse_args(normalize_arguments(sys.argv[1:]))
    try:
        request = request_from_arguments(arguments)
        reply = transmit(request, arguments)
        if arguments.action == 'wait':
            reply = wait_for_event(arguments.event_id, arguments, initial=reply)
        elif arguments.action == 'capture' and arguments.wait and reply.get('status') != 'ERROR':
            reply = wait_for_event(request['event_id'], arguments, initial=reply)
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        return 1 if reply.get('status') in WAIT_FAILURE | {'ERROR'} else 0
    except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; the event may still be running. Query its event ID.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
