#!/usr/bin/env python3
"""Map configured receive PCI addresses to their current Linux interfaces."""

import argparse
import json
from pathlib import Path
import re


PCI_ADDRESS = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')


def map_interfaces(sysfs, pci_addresses):
    """Return one Linux interface for each PCI address, preserving PCI order."""
    root = Path(sysfs) / 'bus/pci/devices'
    interfaces = []
    missing = []
    ambiguous = []
    devices = []
    for address in pci_addresses:
        if not PCI_ADDRESS.fullmatch(address):
            raise ValueError(f'invalid PCI address: {address}')
        net_dir = root / address / 'net'
        names = sorted(item.name for item in net_dir.iterdir()) if net_dir.is_dir() else []
        devices.append({'pci_address': address, 'interfaces': names})
        if not names:
            missing.append(address)
        elif len(names) > 1:
            ambiguous.append(address)
        else:
            interfaces.append(names[0])
    return {
        'interfaces': interfaces,
        'missing_pci_addresses': missing,
        'ambiguous_pci_addresses': ambiguous,
        'devices': devices,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysfs', default='/sys')
    parser.add_argument('pci_addresses', nargs='+')
    arguments = parser.parse_args()
    try:
        report = map_interfaces(arguments.sysfs, arguments.pci_addresses)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
