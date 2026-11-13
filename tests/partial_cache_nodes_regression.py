#!/usr/bin/env python3
"""Verify explicit exclusion of offline cache nodes for debugging."""

from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from controller import Controller

SETTINGS = (ROOT / 'deploy/ansible/group_vars/all/settings.yml').read_text(encoding='utf-8')
INVENTORY = (ROOT / 'deploy/ansible/inventory_example.yml').read_text(encoding='utf-8')
NODES = (ROOT / 'deploy/ansible/templates/nodes.json.j2').read_text(encoding='utf-8')
SERVICE = (ROOT / 'deploy/ansible/templates/controller.service.j2').read_text(encoding='utf-8')
STORAGE = (ROOT / 'deploy/ansible/templates/storage.yaml.j2').read_text(encoding='utf-8')
SITE = (ROOT / 'deploy/ansible/site.yml').read_text(encoding='utf-8')

assert 'tianlai_disabled_cache_nodes: []' in SETTINGS
assert 'tianlai_disabled_cache_nodes: []' in INVENTORY
assert '"enabled": {{ (host not in tianlai_disabled_cache_nodes)' in NODES
assert "tianlai_disabled_cache_nodes | length > 0" in SERVICE
assert '--allow-partial' in SERVICE
service_lines = SERVICE.splitlines()
exec_line = next(line for line in service_lines if line.startswith('ExecStart='))
assert '{%' not in exec_line and service_lines[service_lines.index(exec_line) + 1] == 'Restart=always'
assert 'expectedServerIds:' in STORAGE
assert 'host not in tianlai_disabled_cache_nodes' in STORAGE
assert "difference(groups['cache_nodes'])" in SITE

nodes = [dict(server_id=index, host=f'127.0.0.{index + 1}', port=19000 + index)
         for index in range(14) if index not in (5, 9)]
with tempfile.TemporaryDirectory() as directory:
    controller = Controller(nodes, 'tianlai', directory, allow_partial=True)
    assert controller.allow_partial
    assert [node['server_id'] for node in controller.event_nodes(384)] == [
        index for index in range(14) if index not in (5, 9)]
print('explicit offline cache-node debugging checks passed')
