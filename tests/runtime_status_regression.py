#!/usr/bin/env python3
"""Verify cache runtime phases and the deployed status viewer."""

from pathlib import Path
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
HEADER = (ROOT / 'Common/include/functions.h').read_text(encoding='utf-8')
SOURCE = (ROOT / 'Common/src/runtime_state.cpp').read_text(encoding='utf-8')
EVENT = (ROOT / 'Common/src/event_state.cpp').read_text(encoding='utf-8')
APPLICATION = (ROOT / 'Common/src/application.cpp').read_text(encoding='utf-8')
RECEIVER = (ROOT / 'Common/src/receive_antenna.cpp').read_text(encoding='utf-8')
SENDER = (ROOT / 'Common/src/send_store.cpp').read_text(encoding='utf-8')
STORAGE = (ROOT / 'Storage/storage_receiver.cpp').read_text(encoding='utf-8')
SITE = (ROOT / 'deploy/ansible/site.yml').read_text(encoding='utf-8')
UPDATE = (ROOT / 'deploy/ansible/update_software.yml').read_text(encoding='utf-8')

states = (
    'PREPARING_MEMORY', 'WAITING_FOR_DATA', 'CACHING_DATA', 'PREPARING_EVENT',
    'EVENT_READY', 'SENDING_CACHED_DATA', 'STORAGE_CONFIRMED', 'SEND_FAILED',
    'ERROR', 'STOPPING',
)
for state in states:
    assert state in SOURCE

assert 'RuntimeState::PreparingMemory' in APPLICATION
assert 'RuntimeState::WaitingForData' in APPLICATION
assert 'startupThread = std::thread' in APPLICATION
assert 'para.receiveBuffers = std::move(buffers)' in APPLICATION
assert 'noteAcceptedPacket(*para)' in RECEIVER
assert '\\"runtime_state\\"' in EVENT
assert '\\"runtime_state_age_ms\\"' in EVENT
assert '\\"received_packets\\"' in EVENT
for field in ('lost_packets', 'loss_periods', 'last_loss_period', 'late_packets',
              'lost_by_port'):
    assert f'\\"{field}\\"' in EVENT
assert 'RuntimeState::SendingCachedData' in EVENT
assert 'RuntimeState::StorageConfirmed' in SENDER
for state in ('RECEIVING_AND_WRITING', 'NOTIFYING_BACKEND', 'WAITING_FOR_DATA'):
    assert state in STORAGE
for field in ('listeners_ready', 'active_transfers', 'completed_lane_transfers',
              'written_bytes'):
    assert field in STORAGE
assert 'cache_status.py' in SITE and 'cache_status.py' in UPDATE
assert SITE.count('/usr/local/bin/tlcache') == 2
assert UPDATE.count('/usr/local/bin/tlcache') == 2
assert '/usr/local/bin/tltrigger' not in SITE
assert '/usr/local/bin/tltrigger' not in UPDATE
assert 'trigger.py' not in SITE
assert 'trigger.py' not in UPDATE
assert SITE.count('path: /usr/local/bin/tianlai-status') == 2
assert UPDATE.count('path: /usr/local/bin/tianlai-status') == 2
assert 'cache_status.py --integration-per-trace' in SITE
assert 'cache_status.py --integration-per-trace' in UPDATE
assert 'Install cache status tool on cache nodes' in SITE
assert 'Update cache status tool' in UPDATE
cache_tool = (ROOT / 'tools/cache_status.py').read_text(encoding='utf-8')
display_deploy = (ROOT / 'tools/deploy_tlcache.sh').read_text(encoding='utf-8')
assert 'from tcp_json' not in cache_tool
assert '172.0.0.61' in display_deploy and 'ubuntu' in display_deploy
assert 'sshpass' not in display_deploy and 'ansible_password' not in display_deploy

sys.path.insert(0, str(ROOT / 'tools'))
spec = importlib.util.spec_from_file_location('cache_status', ROOT / 'tools/cache_status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.STATE_TEXT['PREPARING_MEMORY'] == '准备内存'
assert module.STATE_TEXT['SENDING_CACHED_DATA'] == '发送缓存数据'
assert module.STATE_TEXT['RECEIVING_AND_WRITING'] == '接收并写盘'
assert module.DEFAULT_STORAGE_HOST == '172.0.0.142'
assert module.DEFAULT_STORAGE_CONTROL_PORT == 18999
assert 'QUERY_STORAGE' in (ROOT / 'tools/cache_status.py').read_text(encoding='utf-8')
assert 'LOSS_PORTS' in (ROOT / 'tools/cache_status.py').read_text(encoding='utf-8')
table = io.StringIO()
with redirect_stdout(table):
    module.print_table([dict(server_id=1, host='172.0.0.129',
                             runtime_state='CACHING_DATA', initialized_ports=8,
                             receive_ports=8, event_state='IDLE', received_packets=1000,
                             lost_packets=6, late_packets=2, lost_by_port={'3': 6})])
assert 'LOST' in table.getvalue() and '3:6' in table.getvalue()
periods = io.StringIO()
period_results = [
    dict(server_id=0, host='172.0.0.128', runtime_state='CACHING_DATA',
         oldest_period=10000, settled_end=60010),
    dict(server_id=1, host='172.0.0.129', runtime_state='CACHING_DATA',
         oldest_period=10010, settled_end=60000),
]
with redirect_stdout(periods):
    assert module.print_periods(period_results, 50000)
assert ('COMMON range=[10010,60000) latest=59999 trace=1 integration=9999'
        in periods.getvalue())
with tempfile.TemporaryDirectory() as directory:
    status_path = Path(directory) / 'storage.json'
    status_path.write_text(json.dumps(dict(pid=os.getpid(), state='WAITING_FOR_DATA')),
                           encoding='utf-8')
    assert module.storage_snapshot(status_path)['state'] == 'WAITING_FOR_DATA'
    fallback = module.load_nodes(Path(directory) / 'missing-nodes.json')
    assert len(fallback) == 14 and fallback[0]['host'] == '172.0.0.128'

storage_table = io.StringIO()
with redirect_stdout(storage_table):
    module.print_storage(dict(state='WAITING_FOR_DATA', listeners_ready=28,
                              listener_count=28, active_transfers=0,
                              completed_lane_transfers=2, written_bytes=4096))
assert 'STORAGE' in storage_table.getvalue() and '28/28' in storage_table.getvalue()

print('runtime state reporting checks passed')
