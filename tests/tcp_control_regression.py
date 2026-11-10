#!/usr/bin/env python3
"""Verify framed TCP control retries keep the same idempotency key."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from controller import rpc
from tcp_json import receive_frame, send_frame


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(2)
    requests = []

    def serve():
        for attempt in range(2):
            connection, _ = listener.accept()
            with connection:
                request = json.loads(receive_frame(connection))
                requests.append(request)
                if attempt == 0:
                    continue  # Model a reply lost after the request was received.
                reply = dict(version=1, request_id=request['request_id'], server_id=3,
                             status='READY')
                send_frame(connection, json.dumps(reply, separators=(',', ':')).encode())

    worker = threading.Thread(target=serve)
    worker.start()
    result = rpc(dict(server_id=3, host='127.0.0.1', port=listener.getsockname()[1]),
                 'acq', 'QUERY_STATUS', timeout=.05, retries=2)
    worker.join(timeout=2)
    listener.close()
    assert not worker.is_alive() and result['status'] == 'READY'
    assert len(requests) == 2 and requests[0] == requests[1]

    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        nodes = directory / 'nodes.json'
        nodes.write_text(json.dumps([dict(server_id=0, host='127.0.0.1', port=19000)]))
        request = directory / 'request.json'
        request.write_text(json.dumps(dict(version=1, command='QUERY_EVENT', event_id='missing')))
        process = subprocess.Popen([
            sys.executable, str(ROOT / 'tools/controller.py'), '--nodes', str(nodes),
            '--journal', str(directory / 'journal'),
            '--bind', '127.0.0.1', '--port', str(port), '--allow-source', '127.0.0.1',
            '--allow-partial'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            client = subprocess.run([
                sys.executable, str(ROOT / 'tools/trigger.py'), '--host', '127.0.0.1',
                '--port', str(port), '--file', str(request), '--timeout', '.1', '--retries', '8'],
                capture_output=True, text=True, timeout=10)
            assert client.returncode == 1 and json.loads(client.stdout)['status'] == 'ERROR'
        finally:
            process.terminate()
            process.wait(timeout=5)
    print('TCP/JSON framing, reconnect and stable request ID passed')


if __name__ == '__main__':
    main()
