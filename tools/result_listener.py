#!/usr/bin/env python3
"""Example TCP/JSON result receiver; production must durably process before ACK."""
import argparse
import json
import socket
from tcp_json import receive_frame, send_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=19001)
    parser.add_argument('--allow-source', required=True, help='storage management IPv4')
    args = parser.parse_args()
    allowed = socket.gethostbyname(args.allow_source)
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
                    print(json.dumps(notice, ensure_ascii=False), flush=True)
                    # A production backend must persist/process the event before this reply.
                    ack = dict(version=1, command='ACK_RESULT', status='ACKNOWLEDGED',
                               acquisition_id=notice['acquisition_id'], event_id=notice['event_id'])
                except (ConnectionError, OSError, ValueError, KeyError, UnicodeDecodeError):
                    ack = dict(version=1, command='ACK_RESULT', status='ERROR')
                try:
                    send_frame(connection, json.dumps(ack, separators=(',', ':')).encode())
                except (ConnectionError, OSError):
                    pass


if __name__ == '__main__':
    main()
