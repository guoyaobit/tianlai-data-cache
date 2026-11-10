#!/usr/bin/env python3
"""Discover Intel PCI network devices reserved for ixcap without Linux netdevs."""

import argparse
import json
from pathlib import Path
import re
import sys


PCI_ADDRESS = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')


def read_text(path):
    try:
        return path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None


def discover(sysfs, vendor='0x8086'):
    devices = []
    pci_dir = Path(sysfs) / 'bus/pci/devices'
    for device in sorted(pci_dir.iterdir()) if pci_dir.is_dir() else []:
        pci_class = read_text(device / 'class')
        try:
            is_network = pci_class is not None and int(pci_class, 16) >> 16 == 0x02
        except ValueError:
            is_network = False
        net_dir = device / 'net'
        interfaces = sorted(item.name for item in net_dir.iterdir()) if net_dir.is_dir() else []
        driver_link = device / 'driver'
        if (is_network and PCI_ADDRESS.fullmatch(device.name)
                and (read_text(device / 'vendor') or '').lower() == vendor.lower()
                and not interfaces):
            devices.append({
                'pci_address': device.name,
                'driver': driver_link.resolve().name if driver_link.exists() else None,
                'device_id': read_text(device / 'device'),
                'numa_node': read_text(device / 'numa_node'),
            })
    return devices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-count', type=int, required=True)
    parser.add_argument('--vendor', default='0x8086')
    parser.add_argument('--sysfs', default='/sys')
    arguments = parser.parse_args()
    devices = discover(arguments.sysfs, arguments.vendor)
    if len(devices) != arguments.expected_count:
        print(
            f'Expected {arguments.expected_count} Intel receive devices without Linux '
            f'netdevs, found {len(devices)}: {json.dumps(devices)}',
            file=sys.stderr)
        return 1
    print(json.dumps({
        'selection': 'Intel network PCI devices without Linux netdevs; driver is optional',
        'devices': devices,
        'pci_addresses': [item['pci_address'] for item in devices],
    }))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
