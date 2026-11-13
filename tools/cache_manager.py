#!/usr/bin/env python3
"""Root service that accepts cache systemd operations only from the controller."""

import argparse
import json
import socket
import subprocess

from durable_json import ID
from tcp_json import receive_frame, send_frame


ACTIONS = {'START': 'start', 'STOP': 'stop', 'RESTART': 'restart', 'STATUS': 'status'}
SERVICE = 'tianlai-cache.service'


def service_state():
    result = subprocess.run(
        ['/usr/bin/systemctl', 'is-active', SERVICE],
        check=False, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() or 'unknown'


def handle(data):
    request = json.loads(data)
    request_id = request.get('request_id', '')
    if (not isinstance(request, dict) or request.get('version') != 1 or
            not ID.fullmatch(request_id)):
        raise ValueError('invalid cache manager request')
    action = request.get('action')
    if action not in ACTIONS:
        raise ValueError('invalid cache service action')
    command = ACTIONS[action]
    if command != 'status':
        result = subprocess.run(
            ['/usr/bin/systemctl', command, SERVICE],
            check=False, capture_output=True, text=True, timeout=75)
        if result.returncode:
            error = (result.stderr or result.stdout or 'systemctl failed').strip()[:300]
            return dict(version=1, request_id=request_id, status='ERROR',
                        service_state=service_state(), error=error)
    state = service_state()
    expected = (state == 'active' if command in ('start', 'restart')
                else state in ('inactive', 'failed'))
    if command != 'status' and not expected:
        return dict(version=1, request_id=request_id, status='ERROR',
                    service_state=state, error='unexpected service state')
    return dict(version=1, request_id=request_id, status='OK', service_state=state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=19002)
    parser.add_argument('--controller', required=True)
    arguments = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((arguments.bind, arguments.port))
        listener.listen(8)
        while True:
            connection, peer = listener.accept()
            with connection:
                connection.settimeout(90)
                if peer[0] != arguments.controller:
                    continue
                request_id = ''
                try:
                    body = receive_frame(connection)
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        request_id = parsed.get('request_id', '')
                    reply = handle(body)
                except Exception as error:
                    reply = dict(version=1, request_id=request_id, status='ERROR',
                                 service_state=service_state(), error=str(error)[:300])
                send_frame(connection, json.dumps(reply, separators=(',', ':')).encode())


if __name__ == '__main__':
    raise SystemExit(main())
