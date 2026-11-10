#!/usr/bin/env python3
"""Discover receive ports by matching the Intel 10 GbE send-card model."""

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


def slot_address(address):
    """Return a PCI slot without its function number."""
    return re.sub(r'[.-][0-7]$', '', address)


def interface_pci_address(sysfs, interface):
    device = Path(sysfs) / 'class/net' / interface / 'device'
    if not device.exists():
        raise ValueError(f'Interface {interface} has no PCI device')
    address = device.resolve().name
    if not PCI_ADDRESS.fullmatch(address):
        raise ValueError(f'Interface {interface} has invalid PCI address {address}')
    return address


def discover(sysfs, exclude_interface=None, reference_address=None):
    sysfs = Path(sysfs)
    if reference_address is None:
        reference_address = interface_pci_address(sysfs, exclude_interface)
    reference = sysfs / 'bus/pci/devices' / reference_address
    reference_vendor = (read_text(reference / 'vendor') or '').lower()
    reference_device = (read_text(reference / 'device') or '').lower()
    if reference_vendor != '0x8086' or not reference_device:
        raise ValueError(
            f'{exclude_interface or reference_address} is not a valid Intel PCI network device')
    excluded_slot = slot_address(reference_address)
    devices = []
    observed = []
    pci_dir = sysfs / 'bus/pci/devices'
    for device in sorted(pci_dir.iterdir()) if pci_dir.is_dir() else []:
        pci_class = read_text(device / 'class')
        try:
            is_network = pci_class is not None and int(pci_class, 16) >> 16 == 0x02
        except ValueError:
            is_network = False
        net_dir = device / 'net'
        interfaces = sorted(item.name for item in net_dir.iterdir()) if net_dir.is_dir() else []
        driver_link = device / 'driver'
        item = {
            'pci_address': device.name,
            'vendor_id': read_text(device / 'vendor'),
            'device_id': read_text(device / 'device'),
            'driver': driver_link.resolve().name if driver_link.exists() else None,
            'numa_node': read_text(device / 'numa_node'),
            'interfaces': interfaces,
        }
        if (is_network and PCI_ADDRESS.fullmatch(device.name)
                and (item['vendor_id'] or '').lower() == '0x8086'):
            observed.append(item)
        if (is_network and PCI_ADDRESS.fullmatch(device.name)
                and (item['vendor_id'] or '').lower() == reference_vendor
                and (item['device_id'] or '').lower() == reference_device
                and slot_address(device.name) != excluded_slot):
            devices.append(item)
    return devices, {
        'interface': exclude_interface,
        'pci_address': reference_address,
        'excluded_slot': excluded_slot,
        'vendor_id': reference_vendor,
        'device_id': reference_device,
    }, observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-count', type=int, required=True)
    parser.add_argument('--exclude-interface', required=True)
    parser.add_argument('--sysfs', default='/sys')
    arguments = parser.parse_args()
    try:
        devices, reference, observed = discover(arguments.sysfs, arguments.exclude_interface)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    if len(devices) != arguments.expected_count:
        print(
            f'Expected {arguments.expected_count} receive ports matching the Intel '
            f'{arguments.exclude_interface} card model, found {len(devices)}; '
            f'reference={json.dumps(reference)}, matching={json.dumps(devices)}, '
            f'all_intel_network_devices={json.dumps(observed)}',
            file=sys.stderr)
        return 1
    print(json.dumps({
        'selection': 'Intel network PCI devices matching the send-card model, excluding its slot',
        'reference_send_card': reference,
        'all_intel_network_devices': observed,
        'devices': devices,
        'pci_addresses': [item['pci_address'] for item in devices],
    }))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
