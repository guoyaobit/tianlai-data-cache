#!/usr/bin/env python3
"""TLSTORE1 dual-lane reference receiver; production uses the C++ 100 GbE service."""
import argparse
import json
import os
from pathlib import Path
import re
import struct
import threading
import time

CHUNK = struct.Struct('!IIIIQQII')
FRAME = struct.Struct('!QQII')
INPUT_HEADER = struct.Struct('!Q')
IF_TRACE_BIT = 1 << 39
ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
DATA_SYNC_BYTES = 256 * 1024 * 1024
DATA_SYNC_SECONDS = 1.0
MAX_EVENT_MICROSECONDS = 50_000_000


def sync_data(fd):
    """Flush file data, using fsync where fdatasync is unavailable."""
    getattr(os, 'fdatasync', os.fsync)(fd)


def exact(sock, count):
    """Read the requested number of bytes from the current message and report an error if it is incomplete."""
    chunks = bytearray(count)
    view = memoryview(chunks)
    while view:
        n = sock.recv_into(view)
        if not n:
            raise EOFError('connection closed')
        view = view[n:]
    return chunks


def receive_json(sock):
    size, = struct.unpack('!I', exact(sock, 4))
    if not 0 < size <= 1200:
        raise ValueError('invalid JSON length')
    return json.loads(exact(sock, size))


def send_json(sock, value):
    body = json.dumps(value, separators=(',', ':')).encode()
    sock.sendall(struct.pack('!I', len(body)) + body)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    """Sync the temporary file, replace the target, sync the directory, and persist the status marker."""
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def validate_frames(body, first, count, stride, boards, packet_bytes,
                    per_trace, board_begin, packet_id, packets_per_board):
    """Validate each period's bitmap, raw packet headers, and missing-packet placeholders, then return the missing count."""
    missing = 0
    zero_packet = bytes(packet_bytes)
    for i in range(count):
        offset = i * stride
        period, mask, pb, bc = FRAME.unpack_from(body, offset)
        if period != first + i or pb != packet_bytes or bc != boards or mask >> boards:
            raise ValueError('invalid frame metadata')
        missing += boards - mask.bit_count()
        trace, integration = divmod(period, per_trace)
        expected = (15 << 60) | (trace << 40) | (integration << 16)
        for b in range(boards):
            at = offset + FRAME.size + b * packet_bytes
            if mask & (1 << b):
                actual, = INPUT_HEADER.unpack_from(body, at)
                expected_packet = (expected | ((b // packets_per_board + board_begin) << 8) |
                                   (packet_id + b % packets_per_board))
                if actual & ~IF_TRACE_BIT != expected_packet:
                    raise ValueError('raw input header mismatch')
            # Copy only small slices at missing-packet positions so byte comparison runs in C; full data blocks remain zero-copy.
            elif bytes(body[at:at + packet_bytes]) != zero_packet:
                raise ValueError('missing packet slot was not zero-filled')
    return missing


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
    last_frequency = min(first_frequency + 85, 1023)
    return f'frequency-{first_frequency:04d}-{last_frequency:04d}_boards-36-47.bin'


class StoreReceiver:
    def __init__(self, directory, allowed_sources=()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.allowed_sources = set(allowed_sources)
        self.lock = threading.Lock()
        self.active = set()

    def handle(self, sock, address, listen_lane):
        """Validate and persist one node event, then reply STORED after all files are durable."""
        key = None
        files = {}
        try:
            sock.settimeout(120)
            if self.allowed_sources and address[0] not in self.allowed_sources:
                raise ValueError('source address not allowed')
            sock.start_message()
            meta = receive_json(sock)
            sock.finish_message()
            event, acquisition = meta['event_id'], meta['acquisition_id']
            if not ID.fullmatch(event) or not ID.fullmatch(acquisition):
                raise ValueError('invalid event/acquisition identifier')
            server, lane, channels = meta['server_id'], meta['lane'], meta['channel_count']
            mode_mask = (1 << (11 if channels == 288 else 14)) - 1
            server_mask = meta.get('server_mask', mode_mask)
            start, count = meta['start_period'], meta['period_count']
            per_trace, period_us = meta['integration_per_trace'], meta['period_microseconds']
            if (any(type(x) is not int for x in (server, lane, channels, server_mask,
                                                 start, count, per_trace, period_us)) or
                    meta['version'] != 1 or meta['format'] != 'TLSTORE1' or
                    channels not in (288, 384) or
                    not 0 <= server < (11 if channels == 288 else 14) or
                    not server_mask or server_mask & ~mode_mask or not server_mask & (1 << server) or
                    not 0 <= lane < 2 or lane != listen_lane or
                    not 1 <= period_us <= 1000000 or 1000000 % period_us or
                    not 1 <= per_trace <= 0x800000 or not 0 <= start < (1 << 20) * per_trace or
                    not 1 <= count <= MAX_EVENT_MICROSECONDS // period_us or
                    start + count > (1 << 20) * per_trace):
                raise ValueError('invalid stream metadata')
            remote = server >= 11
            ports, boards, packet_bytes = ((8, 36, 2320) if server < 3 else
                                          (4, 24, 8272) if remote else (4, 18, 8848))
            packets_per_board = 2 if remote else 1
            if remote and any(meta.get(key) != value for key, value in dict(
                    profile='remote96', packets_per_board=2, physical_board_count=12,
                    frequency_bins_per_port=86, total_valid_frequencies=1024, fft_per_packet=12).items()):
                raise ValueError('remote packet-pair metadata required')
            if meta['port_count'] != ports or meta['packet_bytes'] != packet_bytes:
                raise ValueError('input profile mismatch')
            selected_ports = list(range(lane, ports, 2))
            if meta.get('ports') != selected_ports:
                raise ValueError('invalid lane port assignment')
            key = (acquisition, event, server, lane)
            with self.lock:
                if any(active[2:] == (server, lane) for active in self.active):
                    key = None
                    raise ValueError('server already receiving')
                self.active.add(key)
            # Persist every newly created directory entry before acknowledging any data.
            path = self.directory
            for part in (acquisition, event, data_directory_name(server)):
                child = path / part
                child.mkdir(exist_ok=True)
                sync_directory(path)
                path = child
            state_path = path / f'lane-{lane}.json'
            old_state = None
            if state_path.exists():
                old_state = json.loads(state_path.read_text())
                old_meta = {key: value for key, value in old_state.items()
                            if key not in ('status', 'missing_packets')}
                if old_meta != meta:
                    raise ValueError('event metadata conflicts with an existing transfer')
            ack = {'version': 1, 'status': 'STORED', 'event_id': event,
                   'acquisition_id': acquisition, 'server_id': server, 'lane': lane}
            if old_state and old_state.get('status') == 'STORED':
                send_json(sock, ack)
                return
            atomic_json(state_path, dict(meta, status='RECEIVING'))
            progress = {p: start for p in selected_ports}
            missing = {p: 0 for p in progress}
            stride = FRAME.size + boards * packet_bytes
            for p in progress:
                filename = path / data_file_name(server, p)
                fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o640)
                files[p] = fd
                os.ftruncate(fd, count * stride)
            unsynced_bytes = 0
            last_sync = time.monotonic()
            while True:
                # A new message grants credit for the preceding validated write.
                # Only the final STORED reply confirms durability.
                sock.start_message()
                kind, port, packet_id, board_begin, first, n, size, reserved = CHUNK.unpack(exact(sock, CHUNK.size))
                if kind == 2:
                    if any((port, packet_id, board_begin, first, n, size, reserved)):
                        raise ValueError('invalid END record')
                    sock.finish_message()
                    break
                if kind != 1 or port not in progress:
                    raise ValueError('invalid chunk type/port')
                expected_id = (server * 8 + port if server < 3 else
                               2 * ((server - 11) * 4 + port) if remote else 24 + (server - 3) * 2 + port // 2)
                expected_begin = 0 if server < 3 else 36 if remote else (port % 2) * 18
                if (packet_id != expected_id or board_begin != expected_begin or first != progress[port] or
                        not 1 <= n <= 64 or first + n > start + count or size != n * stride or reserved):
                    raise ValueError('invalid chunk range/layout')
                body = sock.read_view(size)
                sock.finish_message()
                missing[port] += validate_frames(body, first, n, stride, boards, packet_bytes,
                                                 per_trace, board_begin, packet_id, packets_per_board)
                view = memoryview(body)
                offset = (first - start) * stride
                while view:
                    written = os.pwrite(files[port], view, offset)
                    if not written:
                        raise OSError('zero-length disk write')
                    view = view[written:]
                    offset += written
                progress[port] += n
                unsynced_bytes += size
                now = time.monotonic()
                if unsynced_bytes >= DATA_SYNC_BYTES or now - last_sync >= DATA_SYNC_SECONDS:
                    for fd in files.values():
                        sync_data(fd)
                    unsynced_bytes = 0
                    last_sync = now
                del view, body
            if any(position != start + count for position in progress.values()):
                raise ValueError('incomplete lane transfer')
            for fd in files.values():
                sync_data(fd)
            atomic_json(state_path, dict(meta, status='STORED', missing_packets=missing))
            send_json(sock, ack)
        except Exception as error:
            print(f'receiver {address}: {error}', flush=True)
            sock.fail(error)
        finally:
            for fd in files.values():
                os.close(fd)
            if key is not None:
                with self.lock:
                    self.active.discard(key)
            sock.close()

    def listen(self, address, lane, stop=None):
        from zmq_transport import listen_zmq
        listen_zmq(self, address, lane, stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--bind', required=True, help='40GbE storage IPv4 address')
    parser.add_argument('--port', type=int, default=20000)
    parser.add_argument('--allow-source', action='append', default=[])
    args = parser.parse_args()
    receiver = StoreReceiver(args.directory, args.allow_source)
    receiver.listen((args.bind, args.port), 0)


if __name__ == '__main__':
    main()
