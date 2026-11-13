#!/usr/bin/env python3
"""Durably store completion notices before acknowledging the storage server."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import tempfile

MAX_JSON_BYTES = 1200
LENGTH = struct.Struct('!I')
VALID_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


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


def persist_notice(directory, notice):
    event_id = notice.get('event_id')
    if not isinstance(event_id, str) or not VALID_ID.fullmatch(event_id):
        raise ValueError('invalid event ID')
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f'{event_id}.json'
    if destination.exists():
        if json.loads(destination.read_text(encoding='utf-8')) != notice:
            raise ValueError('event result conflicts with the durable record')
        return destination
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{event_id}.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(notice, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=19001)
    parser.add_argument('--allow-source', required=True, help='storage source IPv4')
    parser.add_argument('--journal', default='./completion-results',
                        help='durable result directory (default: ./completion-results)')
    args = parser.parse_args()
    allowed = socket.gethostbyname(args.allow_source)
    journal = Path(args.journal).resolve()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.bind, args.port))
        sock.listen(16)
        while True:
            connection, source = sock.accept()
            with connection:
                if source[0] != allowed:
                    continue
                connection.settimeout(10)
                try:
                    body = receive_frame(connection)
                    notice = json.loads(body)
                    if (not isinstance(notice, dict) or notice.get('version') != 1 or
                            notice.get('command') != 'CAPTURE_RESULT' or
                            notice.get('status') != 'STORED'):
                        raise ValueError('invalid completion notice')
                    path = persist_notice(journal, notice)
                    print(f'stored event={notice["event_id"]} path={path}', flush=True)
                    ack = dict(version=1, command='ACK_RESULT', status='ACKNOWLEDGED',
                               event_id=notice['event_id'])
                except (ConnectionError, OSError, ValueError, KeyError,
                        UnicodeDecodeError, json.JSONDecodeError) as error:
                    print(f'ERROR: {error}', flush=True)
                    ack = dict(version=1, command='ACK_RESULT', status='ERROR')
                try:
                    send_frame(connection, json.dumps(ack, separators=(',', ':')).encode())
                except (ConnectionError, OSError):
                    pass


if __name__ == '__main__':
    main()
