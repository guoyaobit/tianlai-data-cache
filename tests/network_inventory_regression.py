#!/usr/bin/env python3
"""Exercise read-only NIC inventory using fixture sysfs, without network devices."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('network_inventory', ROOT / 'deploy/ansible/files/network_inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding='utf-8')


def main():
    with tempfile.TemporaryDirectory(prefix='tianlai-nics-') as directory:
        root = Path(directory)
        for name, speed in [('eth0', '10000'), ('eth1', '-1'), ('veth0', None)]:
            nic = root / 'class/net' / name
            nic.mkdir(parents=True)
            write(nic / 'address', '02:00:00:00:00:01')
            write(nic / 'mtu', 9000 if name == 'eth0' else 1500)
            write(nic / 'operstate', 'up' if name == 'eth0' else 'down')
            if speed is not None:
                write(nic / 'speed', speed)
        pci = root / 'bus/pci/devices'
        # Windows cannot create paths containing ':'. The production pattern is
        # checked separately; use equivalent fixture names for this filesystem.
        assert inventory.PCI_ADDRESS.fullmatch('0000:01:00.0')
        address1, address2, address3, address4 = [
            value.replace(':', '_') if os.name == 'nt' else value
            for value in ('0000:0a:00.0', '0000:01:00.1', '0000:02:00.0', '0000:03:00.0')
        ]
        for address, kind in [(address1, '0x020000'), (address2, '0x020000'),
                              (address3, '0x028000'), (address4, '0x030000')]:
            device = pci / address
            write(device / 'class', kind)
            write(device / 'vendor', '0x8086')
            write(device / 'device', '0x1234')
            write(device / 'numa_node', -1 if address.endswith('1') else 0)
        (pci / address1 / 'net/eth0').mkdir(parents=True)
        (pci / address2 / 'net/eth1').mkdir(parents=True)
        addresses = {'eth0': [{'family': 'inet', 'address': '172.0.0.128', 'prefix_length': 24, 'scope': 'global'},
                              {'family': 'inet6', 'address': 'fe80::1', 'prefix_length': 64, 'scope': 'link'}]}
        fixture_pattern = re.compile(r'^[0-9a-fA-F]{4}[_:][0-9a-fA-F]{2}[_:][0-9a-fA-F]{2}\.[0-7]$')
        with patch.object(inventory, 'PCI_ADDRESS', fixture_pattern), patch.object(inventory, 'pci_address', side_effect=lambda device:
                {'eth0': address1, 'eth1': address2}.get(device.parent.name)):
            report = inventory.collect(root, lambda: (addresses, None))
        assert not report['warnings']
        interfaces = {item['name']: item for item in report['interfaces']}
        assert interfaces['eth0']['speed_mbps'] == 10000 and len(interfaces['eth0']['addresses']) == 2
        assert interfaces['eth0']['pci_address'] == address1 and interfaces['eth0']['mtu'] == 9000
        assert interfaces['eth1']['speed_mbps'] is None and interfaces['eth1']['addresses'] == []
        assert interfaces['veth0']['pci_address'] is None
        devices = {item['pci_address']: item for item in report['pci_network_devices']}
        assert len(devices) == 3 and devices[address3]['interfaces'] == []
        assert devices[address2]['numa_node'] is None
        configured = {'receive_pci_addresses': [address1.upper(), '0000:99:00.0'],
                      'send_interfaces': ['eth0'],
                      'send_ips': ['172.0.0.1'],
                      'control_ip': '172.0.0.2', 'storage_ips': ['172.0.0.3']}
        request = {'host': 'cache1', 'roles': ['cache_nodes'], 'configured': configured}
        deployment_report = inventory.build_report(report, request)
        assert deployment_report['host'] == 'cache1' and deployment_report['roles'] == ['cache_nodes']
        assert deployment_report['configured'] == configured
        assert deployment_report['matched_receive_devices'] == [
            {'pci_address': address1.upper(), 'observed': devices[address1]},
            {'pci_address': '0000:99:00.0', 'observed': None},
        ]
        assert deployment_report['matched_send_interfaces'] == [interfaces['eth0']]
        assert deployment_report['interfaces'] == report['interfaces']
        assert deployment_report['pci_network_devices'] == report['pci_network_devices']
        assert deployment_report['warnings'] == report['warnings']
        missing_send = dict(request, configured=dict(configured, send_interfaces=['missing0']))
        assert inventory.build_report(report, missing_send)['matched_send_interfaces'][0] is None
        # Verify the old command-line mode and the new report mode both emit JSON.
        for arguments, expected in [([], report), (['--report', json.dumps(request)], deployment_report)]:
            stdout = io.StringIO()
            with patch.object(inventory, 'collect', return_value=report), \
                    patch.object(sys, 'argv', ['network_inventory.py'] + arguments), \
                    contextlib.redirect_stdout(stdout):
                assert inventory.main() == 0
            assert json.loads(stdout.getvalue()) == expected
        stderr = io.StringIO()
        with patch.object(inventory, 'collect', return_value=report), \
                patch.object(sys, 'argv', ['network_inventory.py', '--report', '{invalid']), \
                contextlib.redirect_stderr(stderr):
            assert inventory.main() == 1
        assert 'Network inventory failed:' in stderr.getvalue()
        report = inventory.collect(root, lambda: (None, 'ip command unavailable'))
        assert report['interfaces'][0]['addresses'] is None and report['warnings']
        assert inventory.build_report(report, request)['warnings'] == report['warnings']
    with patch.object(inventory.subprocess, 'run', side_effect=FileNotFoundError('ip')):
        assert inventory.ip_addresses()[0] is None
    with patch.object(inventory.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps([]), '')):
        assert inventory.ip_addresses() == ({}, None)
    print('network inventory: interfaces, PCI devices and unavailable data passed')


if __name__ == '__main__':
    main()
