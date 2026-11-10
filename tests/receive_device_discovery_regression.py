#!/usr/bin/env python3
"""Verify strict Intel ixcap receive-device discovery."""

import importlib.util
from pathlib import Path
import re
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/ansible/files/discover_receive_devices.py'
SPEC = importlib.util.spec_from_file_location('discover_receive_devices', SCRIPT)
DISCOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISCOVERY)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def add_device(root, address, *, vendor='0x8086', device_id='0x10fb', network=True,
               driver=True, interface=None):
    device = root / 'bus/pci/devices' / address
    write(device / 'class', '0x020000' if network else '0x030000')
    write(device / 'vendor', vendor)
    write(device / 'device', device_id)
    write(device / 'numa_node', '0')
    if driver:
        (device / 'driver').mkdir()
    if interface:
        (device / 'net' / interface).mkdir(parents=True)


def main():
    with tempfile.TemporaryDirectory(prefix='tianlai-receive-discovery-') as directory:
        root = Path(directory)
        receive_addresses = ('pci-01-0', 'pci-01-1', 'pci-02-0', 'pci-02-1')
        for address in receive_addresses:
            add_device(root, address)
        add_device(root, 'pci-03-0', interface='ens2f0')
        add_device(root, 'pci-03-1', interface='ens2f1')
        add_device(root, 'pci-04-0', vendor='0x15b3')
        add_device(root, 'pci-04-1', device_id='0x1533', interface='eno1')
        unbound_address = 'pci-05-0'
        add_device(root, unbound_address, driver=False)
        add_device(root, 'pci-06-0', network=False)
        with patch.object(DISCOVERY, 'PCI_ADDRESS', re.compile(r'^pci-[0-9]{2}-[0-7]$')):
            devices, reference, observed = DISCOVERY.discover(
                root, reference_address='pci-03-0')
        assert [item['pci_address'] for item in devices] == [*receive_addresses, unbound_address]
        assert devices[-1]['driver'] is None
        assert reference['excluded_slot'] == 'pci-03'
        assert reference['device_id'] == '0x10fb'
        assert len(observed) == 8

    inventory = (ROOT / 'deploy/ansible/inventory_example.yml').read_text(encoding='utf-8')
    assert inventory.count('tianlai_receive_devices: auto') == 14
    tasks = (ROOT / 'deploy/ansible/tasks/network_config.yml').read_text(encoding='utf-8')
    assert 'Discover Intel ixcap receive devices' in tasks
    assert '--expected-count {{ 8 if tianlai_server_id | int < 3 else 4 }}' in tasks
    assert '--exclude-interface {{ tianlai_send_interface }}' in tasks
    assert "'CHANGE_ME' in (tianlai_receive_devices" in tasks
    print('Intel ixcap receive-device discovery passed')


if __name__ == '__main__':
    main()
