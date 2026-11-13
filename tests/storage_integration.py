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
import sys
import tempfile
import time

import zmq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from tcp_json import receive_frame, send_frame
CHUNK = struct.Struct('!IIIIQQII')
FRAME = struct.Struct('!QQII')


def data_directory_name(server):
    if server < 3:
        first = server * 8
        return f'packets-{first:02d}-{first + 7:02d}'
    if server < 11:
        first = 24 + (server - 3) * 2
        return f'packets-{first:02d}-{first + 1:02d}'
    first = (server - 11) * 4 * 86
    return f'frequencies-{first:04d}-{min(first + 343, 1023):04d}'


def data_file_name(server, port):
    if server < 3:
        return f'packet-{server * 8 + port:02d}_boards-00-35.bin'
    if server < 11:
        packet = 24 + (server - 3) * 2 + port // 2
        first_board = port % 2 * 18
        return f'packet-{packet:02d}_boards-{first_board:02d}-{first_board + 17:02d}.bin'
    first_frequency = ((server - 11) * 4 + port) * 86
    return (f'frequency-{first_frequency:04d}-{min(first_frequency + 85, 1023):04d}'
            '_boards-36-47.bin')


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


def send_event(context, port, root, server, lane, result_port):
    remote = server >= 11
    ports, boards, packet_bytes = ((8, 36, 2320) if server < 3 else
                                  (4, 24, 8272) if remote else (4, 18, 8848))
    per_board = 2 if remote else 1
    meta = dict(version=1, format='TLSTORE1', acquisition_id='acq', event_id='event',
                server_id=server, lane=lane, channel_count=384, port_count=ports,
                ports=list(range(lane, ports, 2)), start_period=100,
                period_count=1, integration_per_trace=100, period_microseconds=10000,
                packet_bytes=packet_bytes, result_address='127.0.0.1', result_port=result_port)
    if remote:
        meta.update(profile='remote96', packets_per_board=2, physical_board_count=12,
                    frequency_bins_per_port=86, total_valid_frequencies=1024, fft_per_packet=12)
    sock = context.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SNDTIMEO, 30000)
    sock.setsockopt(zmq.RCVTIMEO, 30000)
    sock.connect(f'tcp://127.0.0.{server + 1 + lane * 14};127.0.0.1:{port}')
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
        for p in range(lane, ports, 2):
            packet_id = (server * 8 + p if server < 3 else
                         2 * ((server - 11) * 4 + p) if remote else 24 + (server - 3) * 2 + p // 2)
            board_begin = 0 if server < 3 else 36 if remote else (p % 2) * 18
            body = bytearray(FRAME.pack(100, 1, packet_bytes, boards) + bytes(boards * packet_bytes))
            header = (15 << 60) | (1 << 40) | (board_begin << 8) | packet_id
            struct.pack_into('!Q', body, FRAME.size, header)
            body[FRAME.size + 8:FRAME.size + 16] = b'FPGA-123'
            message = CHUNK.pack(1, p, packet_id, board_begin, 100, 1, len(body), 0) + body
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
    directory = root / 'acq' / 'event' / data_directory_name(server)
    marker = json.loads((directory / f'lane-{lane}.json').read_text())
    for p, body in zip(range(lane, ports, 2), expected):
        assert (directory / data_file_name(server, p)).read_bytes() == body
        assert marker['missing_packets'][str(p)] == boards - 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', help='use a prebuilt test binary instead of make storage')
    parser.add_argument('--test-cli', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    assert data_directory_name(0) == 'packets-00-07'
    assert data_file_name(3, 1) == 'packet-24_boards-18-35.bin'
    assert data_directory_name(13) == 'frequencies-0688-1023'
    assert data_file_name(13, 3) == 'frequency-0946-1023_boards-36-47.bin'
    if args.binary:
        binary = Path(args.binary).resolve()
    else:
        subprocess.run([os.environ.get('MAKE', 'make'), '-C', str(ROOT), 'storage'], check=True)
        binary = ROOT / 'Storage/tianlai-storage'
    with tempfile.TemporaryDirectory(prefix='tianlai-native-store-') as temporary:
        root = Path(temporary)
        port_count = 28
        port = find_port_range(port_count)
        context = zmq.Context()
        backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        backend.bind(('127.0.0.1', 0))
        backend.listen(4)
        backend.settimeout(10)
        notify_port = backend.getsockname()[1]
        if args.test_cli:
            command = [str(binary), '--directory', str(root), '--bind', '127.0.0.1',
                       '--port', str(port), '--receiver-count', str(port_count),
                       '--notify-bind', '127.0.0.1']
        else:
            config = root / 'storage.yaml'
            receivers = '\n'.join(
                f'    - serverId: {server}\n      lane: {lane}\n      bindAddress: "127.0.0.1"\n'
                f'      port: {port + lane * 14 + server}\n'
                f'      allowedSource: "127.0.0.{server + 1}"'
                for server in range(14) for lane in range(2))
            receivers = receivers.replace('    - ', '  - ').replace('\n      ', '\n    ')
            config.write_text(
                f'directory: "{root.as_posix()}"\n'
                f'statusFile: "{(root / "storage-status.json").as_posix()}"\nioThreads: 2\n'
                f'expectedServerIds: {list(range(14))}\n'
                f'receivers:\n{receivers}\n'
                'resultBindAddress: "127.0.0.1"\n', encoding='utf-8')
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
            status_file = root / 'storage-status.json'
            status = json.loads(status_file.read_text())
            assert status['state'] == 'WAITING_FOR_DATA'
            assert status['listeners_ready'] == status['listener_count'] == 28
            with ThreadPoolExecutor(max_workers=28) as pool:
                futures = [pool.submit(send_event, context, port + lane * 14 + server,
                                       root, server, lane, notify_port)
                           for server in range(14) for lane in range(2)]
                for future in futures:
                    future.result(timeout=60)
            connection, _ = backend.accept()
            with connection:
                notice = json.loads(receive_frame(connection))
                send_frame(connection, json.dumps(dict(
                    version=1, command='ACK_RESULT', status='ERROR')).encode())
            assert notice['command'] == 'CAPTURE_RESULT' and notice['status'] == 'STORED'
            assert notice['channel_count'] == 384
            assert notice['acquisition_id'] == 'acq' and notice['event_id'] == 'event'
            assert notice['start'] == {'trace_period_id': 1, 'integration_period_id': 0}
            assert notice['end_exclusive'] == {'trace_period_id': 1, 'integration_period_id': 1}
            assert set(notice['missing_by_server']) == {str(i) for i in range(14)}
            connection, _ = backend.accept()
            with connection:
                repeated = json.loads(receive_frame(connection))
                assert repeated == notice, 'storage did not retry an unacknowledged result'
                send_frame(connection, json.dumps(dict(
                    version=1, command='ACK_RESULT', status='ACKNOWLEDGED',
                    event_id='event')).encode())
            deadline = time.monotonic() + 10
            event_state = root / 'acq/event/event.json'
            while not (event_state.exists() and
                       json.loads(event_state.read_text()).get('backend_acknowledged')):
                if time.monotonic() >= deadline:
                    raise TimeoutError('storage did not persist the backend acknowledgment')
                time.sleep(.05)
            deadline = time.monotonic() + 10
            while True:
                status = json.loads(status_file.read_text())
                if status['state'] == 'WAITING_FOR_DATA' and status['completed_lane_transfers'] == 28:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'storage status did not settle: {status}')
                time.sleep(.05)
            assert status['written_bytes'] > 0 and status['active_transfers'] == 0
            # A lost node-side final reply now returns the durable result at metadata,
            # without clearing the completion marker or rewriting the files.
            send_event(context, port + 3, root, 3, 0, notify_port)
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
    print('C++ storage: 28 lane clients, TCP/JSON completion and retry passed')


if __name__ == '__main__':
    main()
