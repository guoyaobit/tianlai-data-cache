#!/usr/bin/env python3
"""Small Tianlai cache trigger client for status, capture and event queries."""

import argparse
from datetime import datetime, timezone
import json
import re
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
    """Support short capture and generated event ID forms."""
    if not arguments:
        return ['status']
    if arguments == ['--json']:
        return ['status', '--json']
    if arguments[0].lower().endswith('.json'):
        return ['--file', arguments[0]] + arguments[1:]
    if arguments[0].isdigit():
        normalized = ['capture'] + arguments
        if '--wait' not in normalized:
            normalized.append('--wait')
        return normalized
    if (arguments[0].startswith('event-') or
            re.fullmatch(r'\d{8}-\d{6}-[0-9a-fA-F]{6}', arguments[0])):
        return ['event'] + arguments
    return arguments


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
    parser.add_argument('--host', default='172.0.0.142', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=18999, help=argparse.SUPPRESS)
    parser.add_argument('--file', help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=2, help=argparse.SUPPRESS)
    parser.add_argument('--retries', type=int, default=5, help=argparse.SUPPRESS)
    parser.add_argument('--json', action='store_true', help='print the complete JSON reply')
    actions = parser.add_subparsers(dest='action')

    status = actions.add_parser('status', help='query trigger readiness')
    add_channel_options(status)
    status.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='print the complete JSON reply')

    period = actions.add_parser('period', help='show the latest common settled period')
    add_channel_options(period)
    period.add_argument('--integration-per-trace', type=int, default=50000,
                        help=argparse.SUPPRESS)
    period.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='print the complete JSON reply')

    capture = actions.add_parser('capture', help='save the latest settled data')
    capture.add_argument('seconds', type=int, help='duration in seconds (1-50)')
    add_channel_options(capture)
    capture.add_argument('--event-id', help=argparse.SUPPRESS)
    capture.add_argument('--period', dest='start_period', type=int,
                         help='absolute integration period at which capture starts')
    capture.add_argument('--trace', type=int, help=argparse.SUPPRESS)
    capture.add_argument('--integration', type=int, help=argparse.SUPPRESS)
    capture.add_argument('--period-us', type=int, default=200, help=argparse.SUPPRESS)
    capture.add_argument('--integration-per-trace', type=int, default=50000,
                         help=argparse.SUPPRESS)
    capture.add_argument('--wait', action='store_true', help=argparse.SUPPRESS)
    capture.add_argument('--interval', type=float, default=2,
                         help=argparse.SUPPRESS)
    capture.add_argument('--wait-timeout', type=float, default=7200,
                         help=argparse.SUPPRESS)
    capture.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                         help='print the complete JSON reply')

    for name, help_text in (('event', 'query one event'), ('resume', 'resume a paused event'),
                            ('cancel', 'cancel a paused event'),
                            ('wait', 'wait for one event to finish')):
        subcommand = actions.add_parser(name, help=help_text)
        subcommand.add_argument(
            'event_id', nargs='?' if name == 'wait' else None,
            help='optional event ID; current event when omitted' if name == 'wait' else None)
        if name == 'wait':
            subcommand.add_argument('--interval', type=float, default=2,
                                    help=argparse.SUPPRESS)
            subcommand.add_argument('--wait-timeout', type=float, default=7200,
                                    help=argparse.SUPPRESS)
        subcommand.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                                help='print the complete JSON reply')
    return parser


def add_channel_options(parser):
    """Offer both explicit and short channel-count switches."""
    channels = parser.add_mutually_exclusive_group()
    channels.add_argument('--channels', type=int, choices=(288, 384), default=384,
                          help=argparse.SUPPRESS)
    channels.add_argument('--288', dest='channels', action='store_const', const=288,
                          help='use the 288-channel node set')
    channels.add_argument('--384', dest='channels', action='store_const', const=384,
                          help=argparse.SUPPRESS)


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
    return f'{timestamp}-{uuid.uuid4().hex[:6]}'


def capture_request(arguments):
    if type(arguments.seconds) is not int or not 1 <= arguments.seconds <= MAX_CAPTURE_SECONDS:
        raise ValueError(f'seconds must be between 1 and {MAX_CAPTURE_SECONDS}')
    if (arguments.trace is None) != (arguments.integration is None):
        raise ValueError('--trace and --integration must be used together')
    if arguments.start_period is not None and arguments.trace is not None:
        raise ValueError('--period cannot be combined with --trace or --integration')
    if (arguments.start_period is not None and
            (type(arguments.start_period) is not int or arguments.start_period < 0)):
        raise ValueError('--period must be a non-negative integer')
    if (arguments.period_us <= 0 or 1000000 % arguments.period_us or
            not 1 <= arguments.integration_per_trace <= (1 << 23)):
        raise ValueError('invalid acquisition timing')

    period_count = arguments.seconds * 1000000 // arguments.period_us
    if arguments.trace is None:
        status_request = dict(command='QUERY_STATUS', channel_count=arguments.channels)
        status = transmit(status_request, arguments)
        if status.get('status') != 'READY' or not status.get('accept_trigger'):
            print_reply(status, 'status', arguments.json)
            raise RuntimeError('cache is not ready for a trigger')
        oldest, settled_end = status.get('oldest_period'), status.get('settled_end')
        if type(oldest) is not int or type(settled_end) is not int:
            raise RuntimeError('controller did not return a usable period range')
        start_absolute = (settled_end - period_count if arguments.start_period is None
                          else arguments.start_period)
        if start_absolute < oldest or start_absolute + period_count > settled_end:
            raise RuntimeError(
                f'requested range [{start_absolute},{start_absolute + period_count}) is outside '
                f'the common cache range [{oldest},{settled_end})')
        trace, integration = divmod(start_absolute, arguments.integration_per_trace)
    else:
        trace, integration = arguments.trace, arguments.integration

    event_id = arguments.event_id or generated_event_id()
    absolute_start = trace * arguments.integration_per_trace + integration
    print(f'event={event_id} period={absolute_start} start={trace}:{integration} '
          f'duration={arguments.seconds}s channels={arguments.channels}',
          file=sys.stderr)
    request = dict(command='CAPTURE', event_id=event_id, channel_count=arguments.channels,
                   start=dict(trace_period_id=trace, integration_period_id=integration),
                   duration_ms=arguments.seconds * 1000, notify=False)
    return request


def request_from_arguments(arguments):
    if arguments.file:
        if arguments.action:
            raise ValueError('--file cannot be combined with a command')
        payload = json.loads(Path(arguments.file).read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('trigger JSON must be an object')
        if 'command' in payload:
            request = dict(payload)
            if request.get('command') == 'CAPTURE':
                request.setdefault('event_id', generated_event_id())
                request.setdefault('notify', False)
                arguments.file_capture = True
            return request
        allowed = {'seconds', 'period', 'channels'}
        if set(payload) - allowed or 'seconds' not in payload:
            raise ValueError('simple trigger JSON accepts seconds, period and channels')
        arguments.seconds = payload['seconds']
        arguments.start_period = payload.get('period')
        arguments.channels = payload.get('channels', 384)
        if type(arguments.channels) is not int or arguments.channels not in (288, 384):
            raise ValueError('channels must be 288 or 384')
        arguments.event_id = None
        arguments.trace = None
        arguments.integration = None
        arguments.period_us = 200
        arguments.integration_per_trace = 50000
        arguments.file_capture = True
        return capture_request(arguments)
    if arguments.action in ('status', 'period'):
        if (arguments.action == 'period' and
                not 1 <= arguments.integration_per_trace <= (1 << 23)):
            raise ValueError('invalid integration periods per trace')
        request = dict(command='QUERY_STATUS', channel_count=arguments.channels)
        return request
    if arguments.action == 'capture':
        return capture_request(arguments)
    if arguments.action in ('event', 'resume', 'cancel', 'wait'):
        commands = dict(event='QUERY_EVENT', resume='RESUME', cancel='CANCEL')
        if arguments.action == 'wait' and arguments.event_id is None:
            return dict(command='QUERY_ACTIVE_EVENT')
        return dict(command=commands.get(arguments.action, 'QUERY_EVENT'),
                    event_id=arguments.event_id)
    raise ValueError('choose a duration, status, period, event, wait, resume or cancel')


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


def print_reply(reply, action, complete_json=False, integration_per_trace=50000):
    """Print one compact operator result unless full JSON was requested."""
    if complete_json or action is None:
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        return
    status = str(reply.get('status', 'UNKNOWN'))
    if action == 'status':
        channels = reply.get('channel_count', '-')
        nodes = reply.get('participating_server_ids', [])
        if status == 'READY' and reply.get('accept_trigger'):
            print(f"READY channels={channels} nodes={len(nodes)} "
                  f"range=[{reply.get('oldest_period', '-')},{reply.get('settled_end', '-')})")
            return
        counts = reply.get('runtime_state_counts', {})
        details = ','.join(f'{name}:{count}' for name, count in sorted(counts.items()))
        print(f'{status} channels={channels} nodes={len(nodes)}' +
              (f' states={details}' if details else ''))
        return
    if action == 'period':
        oldest = reply.get('oldest_period')
        settled_end = reply.get('settled_end')
        if type(oldest) is int and type(settled_end) is int and oldest < settled_end:
            latest = settled_end - 1
            trace, integration = divmod(latest, integration_per_trace)
            print(f'PERIOD latest={latest} trace={trace} integration={integration} '
                  f'range=[{oldest},{settled_end}) channels={reply.get("channel_count", "-")} '
                  f'nodes={len(reply.get("participating_server_ids", []))}')
        else:
            print(f'{status} period=unavailable')
        return
    event_id = reply.get('event_id', '-')
    message = f'{status} event={event_id}'
    if reply.get('error'):
        message += f" error={reply['error']}"
    print(message)


def main():
    arguments = create_parser().parse_args(normalize_arguments(sys.argv[1:]))
    try:
        arguments.file_capture = False
        request = request_from_arguments(arguments)
        reply = transmit(request, arguments)
        if arguments.action == 'wait' and reply.get('status') != 'ERROR':
            event_id = arguments.event_id or reply.get('event_id')
            if not event_id:
                raise RuntimeError('controller did not return an active event ID')
            reply = wait_for_event(event_id, arguments, initial=reply)
        elif arguments.action == 'capture' and arguments.wait and reply.get('status') != 'ERROR':
            reply = wait_for_event(request['event_id'], arguments, initial=reply)
        elif arguments.file_capture and reply.get('status') != 'ERROR':
            arguments.interval = 2
            arguments.wait_timeout = 7200
            reply = wait_for_event(request['event_id'], arguments, initial=reply)
        output_action = arguments.action or ('capture' if arguments.file_capture else None)
        print_reply(reply, output_action, arguments.json,
                    getattr(arguments, 'integration_per_trace', 50000))
        return 1 if reply.get('status') in WAIT_FAILURE | {'ERROR'} else 0
    except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; the event may still be running. Query its event ID.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
