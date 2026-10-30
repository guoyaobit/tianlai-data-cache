#!/usr/bin/env python3
"""Example UDP backend receiver; production must durably process before ACK."""
import argparse
import json
import socket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=19001)
    parser.add_argument('--allow-source', required=True, help='storage/controller management IPv4')
    args = parser.parse_args()
    allowed = socket.gethostbyname(args.allow_source)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((args.bind, args.port))
        while True:
            body, source = sock.recvfrom(1201)
            if source[0] != allowed:
                continue
            try:
                notice = json.loads(body)
                if (len(body) > 1200 or not isinstance(notice, dict) or
                        notice.get('version') != 1 or
                        notice.get('command') != 'CAPTURE_RESULT' or notice.get('status') != 'STORED'):
                    raise ValueError('invalid completion notice')
                print(json.dumps(notice, ensure_ascii=False), flush=True)
                # A production backend must persist/process the event before this reply.
                ack = dict(version=1, command='ACK_RESULT', status='ACKNOWLEDGED',
                           acquisition_id=notice['acquisition_id'], event_id=notice['event_id'])
            except (ValueError, KeyError, UnicodeDecodeError):
                ack = dict(version=1, command='ACK_RESULT', status='ERROR')
            sock.sendto(json.dumps(ack, separators=(',', ':')).encode(), source)


if __name__ == '__main__':
    main()
