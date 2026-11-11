#!/usr/bin/env python3
"""Verify NUMA-local physical CPU assignment for receive threads."""

import importlib.util
from pathlib import Path
import re
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/ansible/files/assign_receive_cpus.py'
TASKS = (ROOT / 'deploy/ansible/tasks/network_config.yml').read_text(encoding='utf-8')
TEMPLATE = (ROOT / 'deploy/ansible/templates/server.yaml.j2').read_text(encoding='utf-8')

spec = importlib.util.spec_from_file_location('assign_receive_cpus', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding='utf-8')


with tempfile.TemporaryDirectory() as directory:
    sysfs = Path(directory)
    write(sysfs / 'devices/system/cpu/online', '0-15')
    write(sysfs / 'devices/system/node/node0/cpulist', '0-7')
    write(sysfs / 'devices/system/node/node1/cpulist', '8-15')
    for cpu in range(16):
        write(sysfs / f'devices/system/cpu/cpu{cpu}/topology/physical_package_id', cpu // 8)
        write(sysfs / f'devices/system/cpu/cpu{cpu}/topology/core_id', (cpu % 8) // 2)
    addresses = ['pci-01-0', 'pci-01-1', 'pci-02-0', 'pci-02-1']
    for address, node in zip(addresses, [0, 0, 1, 1]):
        write(sysfs / 'bus/pci/devices' / address / 'numa_node', node)
    with patch.object(module, 'PCI_ADDRESS', re.compile(r'^pci-[0-9]{2}-[0-7]$')):
        report = module.assign(sysfs, addresses, reserve_cores=1)
    assert report['cpu_ids'] == [2, 4, 10, 12]
    assert [item['numa_node'] for item in report['assignments']] == [0, 0, 1, 1]

assert 'Assign receive threads to physical CPUs on their NIC NUMA nodes' in TASKS
assert 'tianlai_receive_cpu_ids | unique' in TASKS
assert 'cpuId{{ loop.index0 }}: {{ tianlai_receive_cpu_ids[loop.index0]' in TEMPLATE
print('NUMA-local receive CPU assignment checks passed')
