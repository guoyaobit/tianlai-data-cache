#!/usr/bin/env python3
"""Verify the fixed receive-interface orders on cache nodes 1-11."""

import importlib.util
from pathlib import Path
import re
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / 'deploy/ansible'
SCRIPT = ANSIBLE / 'files/map_interfaces_to_pci.py'
SETTINGS = (ANSIBLE / 'group_vars/all/settings.yml').read_text(encoding='utf-8')
TASKS = (ANSIBLE / 'tasks/network_config.yml').read_text(encoding='utf-8')

spec = importlib.util.spec_from_file_location('map_interfaces_to_pci', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

names = ['ens4f0', 'ens4f1', 'ens5f0', 'ens5f1',
         'ens1f0', 'ens1f1', 'ens3f0', 'ens3f1']
assert 'tianlai_f0_receive_interfaces: [' + ', '.join(names) + ']' in SETTINGS
short_names = ['ens4f0', 'ens4f1', 'ens5f0', 'ens5f1']
assert 'tianlai_f3_10_receive_interfaces: [' + ', '.join(short_names) + ']' in SETTINGS

with tempfile.TemporaryDirectory() as directory:
    sysfs = Path(directory)
    addresses = [f'pci-{index:02d}-0' for index in range(len(names))]
    for name in names:
        (sysfs / 'class/net' / name / 'device').mkdir(parents=True)
    with patch.object(module, 'PCI_ADDRESS', re.compile(r'^pci-[0-9]{2}-[0-7]$')), \
            patch.object(Path, 'resolve', side_effect=[Path(address) for address in addresses]):
        report = module.map_pci_addresses(sysfs, names)
    assert report['pci_addresses'] == addresses
    assert not report['missing_interfaces'] and not report['invalid_interfaces']

assert 'tianlai_server_id | int < 11' in TASKS
assert 'Use the fixed receive-interface order on cache nodes 1-11' in TASKS
assert 'else tianlai_f3_10_receive_interfaces' in TASKS
assert '(tianlai_ordered_receive_report.pci_addresses | sort) == (tianlai_receive_devices | sort)' in TASKS
print('fixed receive-interface order checks passed')
