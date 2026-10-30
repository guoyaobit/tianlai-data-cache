#!/usr/bin/env python3
"""Exercise the C++ storage service with 14 real ZeroMQ clients on Linux."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import zlib

import zmq

ROOT = Path(__file__).resolve().parents[1]
CHUNK = struct.Struct('!IIIIQQII')
FRAME = struct.Struct('!QQII')


def find_port_range(count):
    for base in range(24000, 60000 - count):
        probes = []
        try:
            for port in range(base, base + count):
                probe = socket.socket()
                probe.bind(('127.0.0.1', port))
                probes.append(probe)
            return base
        except OSError:
            pass
        finally:
            for probe in probes:
                probe.close()
    raise RuntimeError('no contiguous TCP port range available')


def send_event(context, port, root, server):
    remote = server >= 11
    ports, boards, packet_bytes = ((8, 36, 2320) if server < 3 else
                                  (4, 24, 8272) if remote else (4, 18, 8848))
    per_board = 2 if remote else 1
    meta = dict(version=1, format='TLSTORE1', acquisition_id='acq', event_id='event',
                server_id=server, lane=0, port_count=ports, start_period=100,
                period_count=1, integration_per_trace=100, period_microseconds=10000,
                packet_bytes=packet_bytes)
    if remote:
        meta.update(profile='remote96', packets_per_board=2, physical_board_count=12,
                    frequency_bins_per_port=86, total_valid_frequencies=1024, fft_per_packet=12)
    sock = context.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SNDTIMEO, 30000)
    sock.setsockopt(zmq.RCVTIMEO, 30000)
    sock.connect(f'tcp://127.0.0.{server + 1};127.0.0.1:{port}')
    try:
        encoded = json.dumps(meta, separators=(',', ':')).encode()
        sock.send(struct.pack('!I', len(encoded)) + encoded)
        first_reply = sock.recv()
        if first_reply != b'OK':
            size = struct.unpack('!I', first_reply[:4])[0]
            result = json.loads(first_reply[4:])
            assert size == len(first_reply) - 4 and result['status'] == 'STORED'
            assert result['server_id'] == server and result['event_id'] == 'event'
            return
        expected = []
        for p in range(ports):
            packet_id = (server * 8 + p if server < 3 else
                         2 * ((server - 11) * 4 + p) if remote else 24 + (server - 3) * 2 + p // 2)
            board_begin = 0 if server < 3 else 36 if remote else (p % 2) * 18
            body = bytearray(FRAME.pack(100, 1, packet_bytes, boards) + bytes(boards * packet_bytes))
            header = (15 << 60) | (1 << 40) | (board_begin << 8) | packet_id
            struct.pack_into('!Q', body, FRAME.size, header)
            body[FRAME.size + 8:FRAME.size + 16] = b'FPGA-123'
            message = CHUNK.pack(1, p, packet_id, board_begin, 100, 1, len(body), zlib.crc32(body)) + body
            sock.send(message)
            assert sock.recv() == b'OK'
            expected.append(body)
        sock.send(CHUNK.pack(2, 0, 0, 0, 0, 0, 0, 0))
        reply = sock.recv()
        assert struct.unpack('!I', reply[:4])[0] == len(reply) - 4
        result = json.loads(reply[4:])
        assert result['status'] == 'STORED' and result['server_id'] == server
    finally:
        sock.close()
    directory = root / 'acq' / 'event' / f'server-{server}'
    marker = json.loads((directory / 'lane-0.stored.json').read_text())
    for p, body in enumerate(expected):
        assert (directory / f'port-{p}.bin').read_bytes() == body
        assert marker['missing_packets'][str(p)] == boards - 1
        assert marker['file_crc32'][str(p)] == zlib.crc32(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', help='use a prebuilt test binary instead of make storage')
    parser.add_argument('--test-cli', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.binary:
        binary = Path(args.binary).resolve()
    else:
        subprocess.run([os.environ.get('MAKE', 'make'), '-C', str(ROOT), 'storage'], check=True)
        binary = ROOT / 'Storage/tianlai-storage'
    with tempfile.TemporaryDirectory(prefix='tianlai-native-store-') as temporary:
        root = Path(temporary)
        port_count = 14
        port = find_port_range(port_count)
        context = zmq.Context()
        backend = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        backend.bind(('127.0.0.1', 0))
        backend.settimeout(10)
        notify_port = backend.getsockname()[1]
        if args.test_cli:
            command = [str(binary), '--directory', str(root), '--bind', '127.0.0.1',
                       '--port', str(port), '--receiver-count', str(port_count),
                       '--notify-address', '127.0.0.1', '--notify-port', str(notify_port),
                       '--notify-bind', '127.0.0.1']
        else:
            config = root / 'storage.yaml'
            receivers = '\n'.join(
                f'    - serverId: {server}\n      port: {port + server}\n'
                f'      allowedSource: "127.0.0.{server + 1}"' for server in range(14))
            config.write_text(
                f'storage:\n  directory: "{root.as_posix()}"\n  bindAddress: "127.0.0.1"\n'
                f'  receivers:\n{receivers}\n'
                f'backend:\n  address: "127.0.0.1"\n  port: {notify_port}\n'
                '  bindAddress: "127.0.0.1"\n', encoding='utf-8')
            command = [str(binary), '--config', str(config)]
        process = subprocess.Popen(command,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Wait for all real TCP listeners; ZeroMQ clients still perform their own handshake.
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'storage service exited: {process.stderr.read()}')
                try:
                    for shard in range(port_count):
                        with socket.create_connection(('127.0.0.1', port + shard), timeout=.1):
                            pass
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('storage service did not start')
                    time.sleep(.05)
            with ThreadPoolExecutor(max_workers=14) as pool:
                futures = [pool.submit(send_event, context, port + server % port_count, root, server)
                           for server in range(14)]
                for future in futures:
                    future.result(timeout=60)
            body, source = backend.recvfrom(1201)
            notice = json.loads(body)
            assert notice['command'] == 'CAPTURE_RESULT' and notice['status'] == 'STORED'
            assert notice['acquisition_id'] == 'acq' and notice['event_id'] == 'event'
            assert notice['start'] == {'trace_period_id': 1, 'integration_period_id': 0}
            assert notice['end_exclusive'] == {'trace_period_id': 1, 'integration_period_id': 1}
            assert set(notice['missing_by_server']) == {str(i) for i in range(14)}
            backend.sendto(json.dumps(dict(version=1, command='ACK_RESULT', status='ERROR')).encode(), source)
            repeated, source = backend.recvfrom(1201)
            assert json.loads(repeated) == notice, 'storage did not retry an unacknowledged result'
            backend.sendto(json.dumps(dict(version=1, command='ACK_RESULT', status='ACKNOWLEDGED',
                                           acquisition_id='acq', event_id='event')).encode(), source)
            deadline = time.monotonic() + 10
            while not (root / 'acq/event/backend-acknowledged.json').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('storage did not persist the backend acknowledgment')
                time.sleep(.05)
            # A lost node-side final reply now returns the durable result at metadata,
            # without clearing the completion marker or rewriting the files.
            send_event(context, port + 3 % port_count, root, 3)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            backend.close()
            context.term()
        if os.name != 'nt':  # Windows terminate() is not a graceful SIGTERM.
            assert process.returncode == 0, process.stderr.read()
    print('C++ storage: 14 clients across 14 dedicated receiver ports, UDP completion and retry passed')


if __name__ == '__main__':
    main()
