#!/usr/bin/env python3
"""Show the storage service and every configured cache node runtime state."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

from tcp_json import receive_frame, send_frame


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


def print_table(results):
    print(f"{'NODE':<5} {'HOST':<16} {'STATE':<22} {'DESCRIPTION':<14} "
          f"{'PORTS':<8} {'EVENT':<13} {'RECEIVED':>12}")
    for item in results:
        state = item.get('runtime_state', 'UNKNOWN')
        ports = (f"{item.get('initialized_ports', 0)}/{item.get('receive_ports', 0)}"
                 if state != 'OFFLINE' else '-')
        print(f"{item['server_id']:<5} {item.get('host', '-'):<16} {state:<22} "
              f"{STATE_TEXT.get(state, '未知状态'):<14} {ports:<8} "
              f"{item.get('event_state', '-'):<13} {item.get('received_packets', 0):>12}")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', default='/opt/tianlai-store/config/nodes.json')
    parser.add_argument('--storage-status', default='/run/tianlai-storage-status.json')
    parser.add_argument('--acquisition', default='tianlai')
    parser.add_argument('--timeout', type=float, default=1.0)
    parser.add_argument('--watch', type=float, default=0,
                        help='refresh interval in seconds; zero prints once')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args()
    nodes = json.loads(Path(arguments.nodes).read_text(encoding='utf-8'))
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('node list is empty or invalid')
    try:
        while True:
            storage = storage_snapshot(arguments.storage_status)
            results = snapshot(nodes, arguments.acquisition, arguments.timeout)
            if arguments.watch and sys.stdout.isatty():
                print('\033[2J\033[H', end='')
            if arguments.json:
                print(json.dumps({'storage': storage, 'cache_nodes': results},
                                 indent=2, ensure_ascii=False))
            else:
                print_storage(storage)
                print_table(results)
            if not arguments.watch:
                unhealthy = storage.get('state') in ('OFFLINE', 'ERROR') or any(
                    item['runtime_state'] in ('OFFLINE', 'ERROR') for item in results)
                return 1 if unhealthy else 0
            time.sleep(max(arguments.watch, 0.2))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
