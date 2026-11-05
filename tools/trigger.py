#!/usr/bin/env python3
"""Send one length-prefixed TCP/JSON request with idempotent retries."""
import argparse
import json
import socket
import uuid
from pathlib import Path
from tcp_json import MAX_JSON_BYTES, receive_frame, send_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=18999)
    parser.add_argument('--file', required=True, help='JSON request; request_id is optional')
    parser.add_argument('--timeout', type=float, default=2)
    parser.add_argument('--retries', type=int, default=5)
    args = parser.parse_args()
    request = json.loads(Path(args.file).read_text())
    request.setdefault('version', 1)
    request.setdefault('request_id', uuid.uuid4().hex)
    body = json.dumps(request, separators=(',', ':')).encode()
    if len(body) > MAX_JSON_BYTES:
        raise ValueError('control message exceeds 1200 bytes')
    peer = (socket.gethostbyname(args.host), args.port)
    for attempt in range(args.retries):
        timeout = args.timeout * min(4, 1 << attempt)
        try:
            with socket.create_connection(peer, timeout=timeout) as sock:
                sock.settimeout(timeout)
                send_frame(sock, body)
                reply = json.loads(receive_frame(sock))
            if reply.get('request_id') == request['request_id']:
                print(json.dumps(reply, indent=2, ensure_ascii=False))
                return 1 if reply.get('status') == 'ERROR' else 0
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
            continue
    raise TimeoutError('no matching TCP reply; query the event before assuming failure')


if __name__ == '__main__':
    raise SystemExit(main())
