#!/usr/bin/env python3
"""Loopback integration of real nodes, TCP/JSON control and ZeroMQ storage."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from controller import Controller, rpc
from store_receiver import StoreReceiver, FRAME


def read_config(path):
    # Shipped examples are flat mappings with JSON-compatible scalar values.
    lines = [line for line in path.read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    result = {}
    for line in lines:
        key, value = line.strip().split(':', 1)
        result[key] = json.loads(value.split('  #', 1)[0].strip())
    return result


def write_config(path, config):
    # JSON is a YAML 1.2 subset and can be parsed by yaml-cpp.
    path.write_text(json.dumps(config), encoding='utf-8')


class DropAckSocket:
    """Simulate one lost durable ACK after the receiver has already fsynced the files."""
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data):
        raise OSError('intentional lost durable acknowledgment')


class DropBlockAckSocket:
    """Suppress the first data-block reply after the receiver finishes the write."""
    def __init__(self, sock):
        self.sock = sock
        self.messages = 0

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def start_message(self):
        self.messages += 1
        if self.messages == 3:
            # The worker has processed the first data message. Suppress its
            # response and let the sender detect the timeout.
            self.sock.pending_reply = False
            raise OSError('intentional lost data-block acknowledgment')
        return self.sock.start_message()


class DropFirstAckReceiver(StoreReceiver):
    def __init__(self, *args):
        super().__init__(*args)
        self.drop_next = True
        self.drop_block_next = True

    def handle(self, sock, address, listen_lane):
        if listen_lane == 0 and self.drop_block_next:
            self.drop_block_next = False
            sock = DropBlockAckSocket(sock)
        elif listen_lane == 0 and self.drop_next:
            self.drop_next = False
            sock = DropAckSocket(sock)
        super().handle(sock, address, listen_lane)


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(fn, timeout=15):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
            if value:
                return value
        except Exception as error:
            last = error
        time.sleep(.02)
    raise AssertionError(f'condition timed out: {last}')


def run(server, directory):
    remote = server >= 11
    variant = 'F0-2' if server < 3 else 'F11-13' if remote else 'F3-10'
    binary = ROOT / variant / ('tianlai-store-' + variant.lower() + '-simulation')
    ports, boards, payload = (8, 36, 2304) if server < 3 else (4, 24, 8256) if remote else (4, 18, 8832)
    cfg = read_config(ROOT / variant / 'receive_config.yaml')
    for key, value in dict(integrationPerTrace=100, periodMicroseconds=10000,
                           halfBufferSeconds=1, maxEventSeconds=1, transitionMiB=8,
                           reserveMemoryMiB=0, prefaultMemory=0, reorderPeriods=1,
                           settleMilliseconds=20, transitionWarnPercent=80,
                           socketTimeoutSeconds=2, chunkPeriods=4).items():
        cfg[key] = value
    write_config(directory / 'recv.yaml', cfg)
    node_config = read_config(ROOT / variant / f'server_{server:02d}.yaml')
    control_port = free_port(socket.SOCK_STREAM)
    simulation = [free_port(socket.SOCK_DGRAM) for _ in range(ports)]
    destinations = [free_port(socket.SOCK_STREAM) for _ in range(2)]
    values = dict(controlBindIp='127.0.0.1', controlPeerIp='127.0.0.1', controlPort=control_port)
    for p in range(ports):
        values[f'simulationPort{p}'] = simulation[p]
    values.update({'sendEthName': '-', 'sendSourceIp': '127.0.0.1',
                   'sendDestinationIp': '127.0.0.1'})
    for lane in range(2):
        values[f'sendDestinationPort{lane}'] = destinations[lane]
    for key, value in values.items():
        node_config[key] = value
    write_config(directory / 'node.yaml', node_config)
    node = dict(server_id=server, host='127.0.0.1', port=control_port)
    stop_receiver = threading.Event()
    log = (directory / 'node.log').open('w')
    process = subprocess.Popen([str(binary), str(directory / 'node.yaml'), str(directory / 'recv.yaml'),
                                '--simulate'], stdout=log, stderr=log)
    controller_process = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def request(command, **fields):
        return rpc(node, 'tianlai', command, **fields)

    def ready_status():
        status = request('QUERY_STATUS')
        return status if status['accept_trigger'] else None

    def initialized_status():
        status = request('QUERY_STATUS')
        return status if status['initialized_ports'] == ports else None

    def make_packet(period, port, local_board):
        trace, integration = divmod(period, 100)
        board_begin = 0 if server < 3 else 36 if remote else (port % 2) * 18
        packet_id = (server * 8 + port if server < 3 else
                     2 * ((server - 11) * 4 + port) + local_board % 2 if remote else
                     24 + (server - 3) * 2 + port // 2)
        board_id = board_begin + (local_board // 2 if remote else local_board)
        head = bytes([0xf0 | (trace >> 16), (trace >> 8) & 255, trace & 255,
                      integration >> 16, (integration >> 8) & 255, integration & 255,
                      board_id, packet_id])
        data = bytearray([(period + port + local_board) & 255]) * payload
        if remote:
            for fft in range(12):
                begin = fft * 86 * 8
                data[begin:begin + 86 * 8] = bytes([(period + port + local_board + fft) & 255]) * (86 * 8)
                if server == 13 and port == 3:
                    data[begin + 78 * 8:begin + 86 * 8] = bytes(8 * 8)
        return head + struct.pack('!Q', period + port) + data

    def expected_slots(period, port):
        if period == 99 and port == 0:
            return ()
        if remote:
            if period == 98:
                return tuple(range(24))  # A complete 96-channel integration, all 24 packets.
            if period == 100:
                return (0, 22, 23)  # Board 36 loses only the second FFT half.
            return (0, 1, 22, 23)
        return (0,) if period == 100 else (0, boards - 1)

    def feed(first, end):
        for period in range(first, end):
            for port in range(ports):
                for board in reversed(expected_slots(period, port)):
                    sock.sendto(make_packet(period, port, board), ('127.0.0.1', simulation[port]))
                if remote and period == 98:
                    sock.sendto(make_packet(period, port, 1), ('127.0.0.1', simulation[port]))
            time.sleep(.001)
        wait_for(lambda: all(request('QUERY_PORT', port=p)['latest_period'] == end - 1 for p in range(ports)))

    try:
        initial = wait_for(initialized_status)
        assert initial['runtime_state'] == 'WAITING_FOR_DATA'
        feed(0, 200)
        status = wait_for(ready_status)
        assert status['runtime_state'] == 'CACHING_DATA'
        assert status['receive_ports'] == ports and status['received_packets'] > 0
        assert status['transition_capacity_periods'] > 0
        assert status['transition_used_periods'] == 0 and not status['transition_warning']
        if not remote:
            for port in range(ports):
                info = request('QUERY_PORT', port=port)
                assert info['packets_per_board'] == 1 and info['packet_slots'] == boards
                assert info['valid_frequency_count'] == (12 if server < 3 else 46)
        if remote:
            for port in range(ports):
                info = request('QUERY_PORT', port=port)
                assert info['packet_slots'] == 24 and info['board_count'] == 12
                assert info['packets_per_board'] == 2 and info['duplicate_packets'] == 1
                assert info['valid_frequency_count'] == (78 if server == 13 and port == 3 else 86)
                # Training flag, out-of-range board IDs and adjacent-port packet ID must be rejected.
                for byte, value in ((0, 0xe0), (6, 35), (6, 48),
                                    (7, 2 * ((server - 11) * 4 + port) + 2)):
                    invalid = bytearray(make_packet(199, port, 0))
                    invalid[byte] = value
                    sock.sendto(invalid, ('127.0.0.1', simulation[port]))
            wait_for(lambda: request('QUERY_STATUS')['ignored_packets'] == ports * 4)
        channels = 384 if remote else 288
        trigger = dict(event_id='crossing', start=dict(trace_period_id=0, integration_period_id=95),
                       period_count=10, channel_count=channels)
        assert request('PREPARE', **trigger)['status'] == 'PREPARING'
        wait_for(lambda: request('QUERY_EVENT', event_id='crossing')['status'] == 'PREPARED')
        assert request('QUERY_STATUS')['runtime_state'] == 'EVENT_READY'
        event = request('QUERY_EVENT', event_id='crossing')
        assert event['channel_count'] == channels
        assert event['missing_packets'] == sum(
            boards - len(expected_slots(period, port)) for port in range(ports) for period in range(95, 105))
        assert event['freeze_ms'] >= 0 and event['transition_capacity_periods'] > 0
        assert request('PREPARE', **trigger)['status'] == 'PREPARED'
        try:
            request('PREPARE', **dict(trigger, period_count=9))
            raise AssertionError('conflicting duplicate trigger accepted')
        except RuntimeError as error:
            assert 'EVENT_ID_CONFLICT' in str(error)
        try:
            request('PREPARE', **dict(trigger, channel_count=384 if channels == 288 else 288))
            raise AssertionError('conflicting channel mode accepted')
        except RuntimeError as error:
            assert ('EVENT_ID_CONFLICT' in str(error) or
                    'CHANNEL_MODE_EXCLUDES_NODE' in str(error))
        try:
            request('RELEASE', event_id='crossing')
            raise AssertionError('release before durable storage accepted')
        except RuntimeError as error:
            assert 'NOT_DURABLY_STORED' in str(error)
        feed(200, 450)  # More than one live-half rotation while the event is frozen.
        request('STORE', event_id='crossing')
        wait_for(lambda: request('QUERY_EVENT', event_id='crossing')['status'] == 'SEND_FAILED')
        assert request('QUERY_STATUS')['runtime_state'] == 'SEND_FAILED'
        receiver = DropFirstAckReceiver(directory / 'disk', ['127.0.0.1'])
        threads = [threading.Thread(target=receiver.listen,
                   args=(('127.0.0.1', destinations[lane]), lane, stop_receiver), daemon=True) for lane in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(.1)
        request('STORE', event_id='crossing')
        wait_for(lambda: request('QUERY_EVENT', event_id='crossing')['status'] == 'SEND_FAILED')
        assert request('QUERY_STATUS')['status'] == 'BUSY'
        request('STORE', event_id='crossing')
        wait_for(lambda: request('QUERY_EVENT', event_id='crossing')['status'] == 'SEND_FAILED')
        assert request('QUERY_STATUS')['status'] == 'BUSY'
        request('STORE', event_id='crossing')
        wait_for(lambda: request('QUERY_EVENT', event_id='crossing')['status'] == 'STORED')
        assert request('QUERY_STATUS')['runtime_state'] == 'STORAGE_CONFIRMED'
        request('RELEASE', event_id='crossing')
        assert request('RELEASE', event_id='crossing')['status'] == 'RELEASED'
        assert request('QUERY_STATUS')['runtime_state'] == 'CACHING_DATA'
        for port in range(ports):
            path = directory / 'disk/tianlai/crossing' / f'server-{server}/port-{port}.bin'
            content = path.read_bytes()
            stride = 24 + boards * (payload + 16)
            assert len(content) == 10 * stride
            for i, period in enumerate(range(95, 105)):
                mask = sum(1 << b for b in expected_slots(period, port))
                assert FRAME.unpack_from(content, i * stride) == (period, mask, payload + 16, boards)
                for board in range(boards):
                    at = i * stride + 24 + board * (payload + 16)
                    expected = make_packet(period, port, board) if mask & (1 << board) else bytes(payload + 16)
                    assert content[at:at + payload + 16] == expected
        event_path = directory / 'disk/tianlai/crossing' / f'server-{server}'
        stored = [json.loads((event_path / f'lane-{lane}.stored.json').read_text())
                  for lane in range(2)]
        assert set(stored[0]['missing_packets']) == {str(p) for p in range(0, ports, 2)}
        assert set(stored[1]['missing_packets']) == {str(p) for p in range(1, ports, 2)}
        if remote:
            meta = json.loads((directory / 'disk/tianlai/crossing' /
                               f'server-{server}/lane-0.meta.json').read_text())
            assert meta['profile'] == 'remote96' and meta['packets_per_board'] == 2
            assert meta['physical_board_count'] == 12 and meta['frequency_bins_per_port'] == 86
        feed(450, 650)
        wait_for(lambda: request('QUERY_STATUS')['accept_trigger'])
        for suffix, start, count, error_code in (
                ('expired', 0, 10, 'EXPIRED'), ('future', 700, 10, 'NOT_READY'),
                ('too-long', 550, 101, 'INVALID_RANGE')):
            trace, integration = divmod(start, 100)
            try:
                request('PREPARE', event_id=suffix,
                        start=dict(trace_period_id=trace, integration_period_id=integration), period_count=count)
                raise AssertionError('invalid range accepted')
            except RuntimeError as error:
                assert error_code in str(error)
        # Cancellation is idempotent even when it arrives before PREPARE (UDP reordering).
        assert request('CANCEL', event_id='cancel-first')['status'] == 'CANCELLED'
        try:
            request('PREPARE', event_id='cancel-first', start=dict(trace_period_id=5, integration_period_id=95),
                    period_count=10)
            raise AssertionError('cancelled event resurrected')
        except RuntimeError as error:
            assert 'EVENT_ID_CONFLICT' in str(error)
        controller = Controller([node], 'tianlai', directory / 'journal', allow_partial=True,
                                integration_per_trace=100, period_microseconds=10000)
        message = dict(version=1, acquisition_id='tianlai', command='CAPTURE', event_id='controller-event',
                       start=dict(trace_period_id=5, integration_period_id=95), period_count=10)
        controller.dispatch(message)
        result = wait_for(lambda: controller.events['controller-event']
                          if controller.events['controller-event']['status'] in ('COMPLETED', 'PAUSED') else None)
        assert result['status'] == 'COMPLETED', result
        assert controller.dispatch(message)['status'] == 'COMPLETED'
        # Verify the external TCP/JSON entry point and command-line trigger client.
        (directory / 'nodes.json').write_text(json.dumps([node]))
        (directory / 'query.json').write_text(json.dumps(dict(version=1, acquisition_id='tianlai',
                                                             command='QUERY_STATUS')))
        external_port = free_port(socket.SOCK_STREAM)
        controller_process = subprocess.Popen([
            'python3', str(ROOT / 'tools/controller.py'), '--nodes', str(directory / 'nodes.json'),
            '--acquisition', 'tianlai', '--journal', str(directory / 'external-journal'),
            '--bind', '127.0.0.1', '--port', str(external_port), '--allow-source', '127.0.0.1', '--allow-partial',
            '--integration-per-trace', '100', '--period-us', '10000'],
            stdout=log, stderr=log)
        result = subprocess.run(['python3', str(ROOT / 'tools/trigger.py'), '--host', '127.0.0.1',
            '--port', str(external_port), '--file', str(directory / 'query.json'), '--timeout', '.5'],
            capture_output=True, text=True, timeout=10, check=True)
        assert json.loads(result.stdout)['status'] == 'BUSY_OR_WARMING'
        print(f'{variant}: UDP data input, TCP/JSON control, ZeroMQ retry and controller passed')
    except Exception:
        log.flush()
        print((directory / 'node.log').read_text())
        raise
    finally:
        sock.close()
        stop_receiver.set()
        if controller_process is not None:
            controller_process.terminate()
            controller_process.wait(timeout=10)
        process.terminate()
        process.wait(timeout=10)
        log.close()


def main():
    subprocess.run([os.environ.get('MAKE', 'make'), '-C', str(ROOT), '-j2', 'simulation'], check=True,
                   stdout=subprocess.DEVNULL)
    for variant in ('F0-2', 'F3-10', 'F11-13'):
        binary = ROOT / variant / ('tianlai-store-' + variant.lower() + '-simulation')
        for cfg in (ROOT / variant).glob('server_*.yaml'):
            result = subprocess.run([str(binary), str(cfg), str(ROOT / variant / 'receive_config.yaml'), '--check-config'],
                                    check=True, capture_output=True, text=True)
            server = int(cfg.stem.split('_')[-1])
            assert f'server={server}' in result.stdout
            assert 'transport=ZeroMQ lanes=2' in result.stdout
            assert 'rateLimit=none' in result.stdout
            if server < 3:
                for port in range(8):
                    assert f'port={port} packetId={server * 8 + port} boards=0..35' in result.stdout
            elif server < 11:
                for port in range(4):
                    pid, begin = 24 + (server - 3) * 2 + port // 2, (port % 2) * 18
                    assert f'port={port} packetId={pid} boards={begin}..{begin + 17}' in result.stdout
            else:
                allocation = int(result.stdout.split('estimatedBytes=')[1].split()[0])
                assert 431.4e9 < allocation < 431.6e9, allocation
                for port in range(4):
                    global_port = (server - 11) * 4 + port
                    assert f'port={port} packetId={global_port * 2} boards=36..47' in result.stdout
                    assert (f'packetIdEnd={global_port * 2 + 1} packetsPerFrame=24 '
                            f'frequencyBegin={global_port * 86} frequencyCount=86 '
                            f'validFrequencyCount={78 if global_port == 11 else 86}') in result.stdout
    with tempfile.TemporaryDirectory(prefix='tianlai-store-loopback-') as temp:
        # Reject the removed limiter so deployments cannot silently retain old behavior.
        for variant, server in (('F0-2', 0), ('F3-10', 3), ('F11-13', 11)):
            cfg = read_config(ROOT / variant / 'receive_config.yaml')
            cfg['sendRateBitsPerSecond'] = 5600000000
            bad = Path(temp) / 'invalid.yaml'
            write_config(bad, cfg)
            result = subprocess.run([str(ROOT / variant / ('tianlai-store-' + variant.lower() + '-simulation')),
                str(ROOT / variant / f'server_{server:02d}.yaml'), str(bad), '--check-config'],
                capture_output=True, text=True)
            assert result.returncode != 0 and 'sendRateBitsPerSecond' in result.stderr, result
        for server in (0, 3, 11, 13):
            path = Path(temp) / f'server-{server}'
            path.mkdir()
            run(server, path)


if __name__ == '__main__':
    main()
