#!/usr/bin/env python3
"""Show cache-node status or the common settled integration-period range."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
import uuid

DEFAULT_CONTROL_PORT = 19000
DEFAULT_STORAGE_HOST = '172.0.0.142'
DEFAULT_STORAGE_CONTROL_PORT = 18999
MAX_JSON_BYTES = 1200
LENGTH = struct.Struct('!I')
DEFAULT_CACHE_NODES = [
    dict(server_id=server_id, host=f'172.0.0.{128 + server_id}',
         port=DEFAULT_CONTROL_PORT)
    for server_id in range(14)
]


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

STATE_TEXT = {
    'STARTING': '启动中',
    'PREPARING_MEMORY': '准备内存',
    'WAITING_FOR_DATA': '等待接收数据',
    'CACHING_DATA': '缓存数据中',
    'PREPARING_EVENT': '准备触发数据',
    'EVENT_READY': '触发数据已准备',
    'SENDING_CACHED_DATA': '发送缓存数据',
    'STORAGE_CONFIRMED': '存储已确认',
    'SEND_FAILED': '发送失败',
    'ERROR': '程序错误',
    'STOPPING': '停止中',
    'OFFLINE': '无法连接',
    'RECEIVING_AND_WRITING': '接收并写盘',
    'NOTIFYING_BACKEND': '通知消色散后端',
}


def query(node, acquisition, timeout):
    request = dict(version=1, request_id=uuid.uuid4().hex,
                   acquisition_id=acquisition, command='QUERY_STATUS')
    body = json.dumps(request, separators=(',', ':')).encode()
    try:
        with socket.create_connection((node['host'], node.get('port', 19000)),
                                      timeout=timeout) as connection:
            connection.settimeout(timeout)
            send_frame(connection, body)
            reply = json.loads(receive_frame(connection))
        if reply.get('request_id') != request['request_id']:
            raise ValueError('request ID mismatch')
        return dict(reply, host=node['host'])
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError) as error:
        return dict(server_id=node['server_id'], host=node['host'], status='OFFLINE',
                    runtime_state='OFFLINE', error=str(error))


def snapshot(nodes, acquisition, timeout):
    with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
        results = list(workers.map(lambda node: query(node, acquisition, timeout), nodes))
    return sorted(results, key=lambda item: item['server_id'])


def remote_storage_snapshot(host, port, acquisition, timeout):
    request = dict(version=1, request_id=uuid.uuid4().hex,
                   acquisition_id=acquisition, command='QUERY_STORAGE')
    body = json.dumps(request, separators=(',', ':')).encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            send_frame(connection, body)
            reply = json.loads(receive_frame(connection))
        if reply.get('request_id') != request['request_id']:
            raise ValueError('request ID mismatch')
        status = reply.get('storage')
        if not isinstance(status, dict) or status.get('service') != 'tianlai-storage':
            raise ValueError('invalid storage status reply')
        return status
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError) as error:
        return dict(service='tianlai-storage', state='OFFLINE', error=str(error),
                    listeners_ready=0, listener_count=0, active_transfers=0,
                    completed_lane_transfers=0, written_bytes=0)


def load_nodes(path):
    try:
        nodes = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        nodes = DEFAULT_CACHE_NODES
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('node list is empty or invalid')
    return nodes


def print_table(results):
    print(f"{'NODE':<5} {'HOST':<16} {'STATE':<22} {'PORTS':<8} {'EVENT':<13} "
          f"{'RECEIVED':>12} {'LOST':>10} {'LATE':>10} {'LOSS_PORTS'}")
    for item in results:
        state = item.get('runtime_state', 'UNKNOWN')
        ports = (f"{item.get('initialized_ports', 0)}/{item.get('receive_ports', 0)}"
                 if state != 'OFFLINE' else '-')
        losses = item.get('lost_by_port', {})
        loss_ports = ','.join(f'{port}:{count}' for port, count in
                              sorted(losses.items(), key=lambda entry: int(entry[0]))) or '-'
        print(f"{item['server_id']:<5} {item.get('host', '-'):<16} {state:<22} "
              f"{ports:<8} {item.get('event_state', '-'):<13} "
              f"{item.get('received_packets', 0):>12} {item.get('lost_packets', 0):>10} "
              f"{item.get('late_packets', 0):>10} {loss_ports}")


def print_periods(results, integration_per_trace):
    print(f"{'NODE':<5} {'HOST':<16} {'STATE':<22} {'OLDEST':>12} "
          f"{'SETTLED_END':>12} {'LATEST':>12} {'TRACE':>8} {'INTEGRATION':>12}")
    online = []
    for item in results:
        state = item.get('runtime_state', 'UNKNOWN')
        oldest = item.get('oldest_period')
        end = item.get('settled_end')
        usable = (state != 'OFFLINE' and type(oldest) is int and type(end) is int and
                  oldest < end)
        if usable:
            latest = end - 1
            trace, integration = divmod(latest, integration_per_trace)
            online.append(item)
            values = (str(oldest), str(end), str(latest), str(trace), str(integration))
        else:
            values = ('-', '-', '-', '-', '-')
        print(f"{item['server_id']:<5} {item.get('host', '-'):<16} {state:<22} "
              f"{values[0]:>12} {values[1]:>12} {values[2]:>12} "
              f"{values[3]:>8} {values[4]:>12}")

    if len(online) != len(results) or not online:
        print('COMMON unavailable: one or more nodes are offline or have no settled data')
        return False
    oldest = max(item['oldest_period'] for item in online)
    end = min(item['settled_end'] for item in online)
    if oldest >= end:
        print('COMMON unavailable: nodes have no overlapping settled period range')
        return False
    latest = end - 1
    trace, integration = divmod(latest, integration_per_trace)
    print(f'COMMON range=[{oldest},{end}) latest={latest} '
          f'trace={trace} integration={integration}')
    return True


def storage_snapshot(path):
    try:
        status = json.loads(Path(path).read_text(encoding='utf-8'))
        pid = status.get('pid')
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid storage PID')
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass  # An unprivileged monitor cannot signal the root-owned service.
        return status
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return dict(service='tianlai-storage', state='OFFLINE', error=str(error),
                    listeners_ready=0, listener_count=0, active_transfers=0,
                    completed_lane_transfers=0, written_bytes=0)


def print_storage(status):
    state = status.get('state', 'OFFLINE')
    listeners = f"{status.get('listeners_ready', 0)}/{status.get('listener_count', 0)}"
    print('STORAGE')
    print(f"  state={state} ({STATE_TEXT.get(state, '未知状态')}) "
          f"listeners={listeners} active_transfers={status.get('active_transfers', 0)} "
          f"completed_lanes={status.get('completed_lane_transfers', 0)} "
          f"written_bytes={status.get('written_bytes', 0)}")
    error = status.get('last_error') or status.get('error')
    if error:
        print(f"  last_error={error}")
    print('CACHE NODES')


def local_storage_snapshot(path):
    if path == '-':
        return None
    status_path = Path(path)
    service = Path('/etc/systemd/system/tianlai-storage.service')
    if not status_path.exists() and not service.exists():
        return None
    return storage_snapshot(status_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('status', 'period'), default='status')
    parser.add_argument('--nodes', default='/opt/tianlai-store/config/nodes.json')
    parser.add_argument('--storage-status', default='/run/tianlai-storage-status.json')
    parser.add_argument('--storage-host', default=DEFAULT_STORAGE_HOST)
    parser.add_argument('--storage-port', type=int, default=DEFAULT_STORAGE_CONTROL_PORT)
    parser.add_argument('--acquisition', default='tianlai')
    parser.add_argument('--integration-per-trace', type=int, default=50000)
    parser.add_argument('--timeout', type=float, default=1.0)
    parser.add_argument('--watch', type=float, default=0,
                        help='refresh interval in seconds; zero prints once')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args()
    if not 1 <= arguments.integration_per_trace <= (1 << 23):
        raise ValueError('invalid integration periods per trace')
    nodes = load_nodes(arguments.nodes)
    try:
        while True:
            storage = remote_storage_snapshot(arguments.storage_host, arguments.storage_port,
                                              arguments.acquisition, arguments.timeout)
            local_storage = local_storage_snapshot(arguments.storage_status)
            if storage.get('state') == 'OFFLINE' and local_storage is not None:
                storage = local_storage
            results = snapshot(nodes, arguments.acquisition, arguments.timeout)
            if arguments.watch and sys.stdout.isatty():
                print('\033[2J\033[H', end='')
            if arguments.json:
                print(json.dumps({'storage': storage, 'cache_nodes': results},
                                 indent=2, ensure_ascii=False))
                period_ready = True
            elif arguments.command == 'period':
                print_storage(storage)
                period_ready = print_periods(results, arguments.integration_per_trace)
            else:
                print_storage(storage)
                print_table(results)
                period_ready = True
            if not arguments.watch:
                unhealthy = storage.get('state') in ('OFFLINE', 'ERROR') or any(
                    item['runtime_state'] in ('OFFLINE', 'ERROR') for item in results)
                return 1 if unhealthy or not period_ready else 0
            time.sleep(max(arguments.watch, 0.2))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
