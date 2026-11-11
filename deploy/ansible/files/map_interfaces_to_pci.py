#!/usr/bin/env python3
"""Resolve ordered Linux interface names to PCI addresses."""

import argparse
import json
from pathlib import Path
import re


PCI_ADDRESS = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')


def map_pci_addresses(sysfs, interfaces):
    """Resolve interface device links while preserving the requested order."""
    root = Path(sysfs) / 'class/net'
    addresses = []
    missing = []
    invalid = []
    devices = []
    for interface in interfaces:
        device = root / interface / 'device'
        if not device.exists():
            missing.append(interface)
            devices.append({'interface': interface, 'pci_address': None})
            continue
        address = device.resolve().name
        if not PCI_ADDRESS.fullmatch(address):
            invalid.append(interface)
            devices.append({'interface': interface, 'pci_address': address})
            continue
        addresses.append(address)
        devices.append({'interface': interface, 'pci_address': address})
    return {
        'interfaces': interfaces,
        'pci_addresses': addresses,
        'missing_interfaces': missing,
        'invalid_interfaces': invalid,
        'devices': devices,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysfs', default='/sys')
    parser.add_argument('interfaces', nargs='+')
    arguments = parser.parse_args()
    print(json.dumps(map_pci_addresses(arguments.sysfs, arguments.interfaces), sort_keys=True))


if __name__ == '__main__':
    main()
