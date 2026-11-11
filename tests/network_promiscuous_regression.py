#!/usr/bin/env python3
"""Verify deployment-time promiscuous mode for every receive PCI interface."""

import importlib.util
from pathlib import Path
import re
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / 'deploy/ansible'
MAPPER = ANSIBLE / 'files/map_receive_interfaces.py'
TASKS = (ANSIBLE / 'tasks/network_config.yml').read_text(encoding='utf-8')
DISPATCHER = (ANSIBLE / 'templates/tianlai-receive-promisc.j2').read_text(encoding='utf-8')
RECEIVER = (ROOT / 'Common/src/receive_antenna.cpp').read_text(encoding='utf-8')

spec = importlib.util.spec_from_file_location('map_receive_interfaces', MAPPER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as directory:
    sysfs = Path(directory)
    for pci, interface in [('pci-19-0', 'ens3f0'), ('pci-19-1', 'ens3f1')]:
        (sysfs / 'bus/pci/devices' / pci / 'net' / interface).mkdir(parents=True)
    with patch.object(module, 'PCI_ADDRESS', re.compile(r'^pci-[0-9]{2}-[0-7]$')):
        report = module.map_interfaces(sysfs, ['pci-19-0', 'pci-19-1'])
        assert report['interfaces'] == ['ens3f0', 'ens3f1']
        assert not report['missing_pci_addresses'] and not report['ambiguous_pci_addresses']
        missing = module.map_interfaces(sysfs, ['pci-20-0'])
        assert missing['missing_pci_addresses'] == ['pci-20-0']

assert 'ip, link, set, dev, "{{ item }}", promisc, \'on\'' in TASKS
assert 'Require promiscuous mode on every receive interface' in TASKS
assert 'dispatcher.d/20-tianlai-receive-promisc' in TASKS
assert 'tianlai_receive_devices' in DISPATCHER and 'promisc on' in DISPATCHER
assert 'ixcap_set_promisc' not in RECEIVER
print('receive network promiscuous-mode checks passed')
