#!/usr/bin/env python3
"""Send one UDP JSON request to the controller, with request-ID matching and retries."""
import argparse
import json
import socket
import time
import uuid
from pathlib import Path


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
    if len(body) > 1200:
        raise ValueError('control message exceeds 1200 bytes')
    peer = (socket.gethostbyname(args.host), args.port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _ in range(args.retries):
            sock.sendto(body, peer)
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    data, address = sock.recvfrom(1201)
                except socket.timeout:
                    break
                if address != peer or len(data) > 1200:
                    continue
                reply = json.loads(data)
                if reply.get('request_id') == request['request_id']:
                    print(json.dumps(reply, indent=2, ensure_ascii=False))
                    return 1 if reply.get('status') == 'ERROR' else 0
    raise TimeoutError('no matching UDP reply; query the event before assuming failure')


if __name__ == '__main__':
    raise SystemExit(main())
