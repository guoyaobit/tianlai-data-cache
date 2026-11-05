#!/usr/bin/env python3
"""Read Linux network interfaces and PCI network devices without changing them."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PCI_ADDRESS = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')


def read_text(path):
    try:
        return path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None


def number(value, *, minimum=None):
    try:
        result = int(value)
        return result if minimum is None or result >= minimum else None
    except (ValueError, TypeError):
        return None


def driver_name(device):
    driver = device / 'driver'
    return driver.resolve().name if driver.exists() else None


def pci_address(device):
    if not device.exists():
        return None
    name = device.resolve().name
    return name if PCI_ADDRESS.fullmatch(name) else None


def ip_addresses():
    try:
        result = subprocess.run(['ip', '-j', 'address', 'show'], capture_output=True,
                                text=True, check=True, timeout=15)
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            raise ValueError('ip JSON is not a list')
        return {entry['ifname']: [
            {'family': item.get('family'), 'address': item.get('local'),
             'prefix_length': item.get('prefixlen'), 'scope': item.get('scope')}
            for item in entry.get('addr_info', []) if item.get('family') in ('inet', 'inet6')
        ] for entry in data if isinstance(entry, dict) and 'ifname' in entry}, None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        return None, str(error)


def collect(sysfs=Path('/sys'), address_reader=ip_addresses):
    sysfs = Path(sysfs)
    interface_dir = sysfs / 'class/net'
    pci_dir = sysfs / 'bus/pci/devices'
    addresses, ip_error = address_reader()
    warnings = []
    if ip_error:
        warnings.append('IP addresses unavailable: ' + ip_error)
    if not interface_dir.is_dir():
        warnings.append('Network interface sysfs unavailable')
    if not pci_dir.is_dir():
        warnings.append('PCI device sysfs unavailable')

    interfaces = []
    for interface in sorted(interface_dir.iterdir()) if interface_dir.is_dir() else []:
        device = interface / 'device'
        interfaces.append({
            'name': interface.name,
            'mac': read_text(interface / 'address'),
            'addresses': None if addresses is None else addresses.get(interface.name, []),
            'mtu': number(read_text(interface / 'mtu'), minimum=0),
            'state': read_text(interface / 'operstate'),
            'speed_mbps': number(read_text(interface / 'speed'), minimum=1),
            'pci_address': pci_address(device),
            'driver': driver_name(device),
            'numa_node': number(read_text(device / 'numa_node'), minimum=0),
        })

    devices = []
    for device in sorted(pci_dir.iterdir()) if pci_dir.is_dir() else []:
        pci_class = read_text(device / 'class')
        try:
            is_network = pci_class is not None and int(pci_class, 16) >> 16 == 0x02
        except ValueError:
            is_network = False
        if not is_network or not PCI_ADDRESS.fullmatch(device.name):
            continue
        net_dir = device / 'net'
        devices.append({
            'pci_address': device.name,
            'pci_class': pci_class,
            'vendor_id': read_text(device / 'vendor'),
            'device_id': read_text(device / 'device'),
            'driver': driver_name(device),
            'numa_node': number(read_text(device / 'numa_node'), minimum=0),
            'interfaces': sorted(entry.name for entry in net_dir.iterdir()) if net_dir.is_dir() else [],
        })
    return {'interfaces': interfaces, 'pci_network_devices': devices, 'warnings': warnings}


def build_report(inventory, request):
    """Match configured receive PCI addresses and send interfaces to observations."""
    if not isinstance(request, dict) or not isinstance(request.get('configured'), dict):
        raise ValueError('report request must contain a configured object')
    configured = request['configured']
    receive_addresses = configured.get('receive_pci_addresses', [])
    if not isinstance(receive_addresses, list) or any(not isinstance(item, str) for item in receive_addresses):
        raise ValueError('receive_pci_addresses must be a list of strings')
    send_interfaces = configured.get('send_interfaces', [])
    if not isinstance(send_interfaces, list) or any(not isinstance(item, str) for item in send_interfaces):
        raise ValueError('send_interfaces must be a list of strings')

    devices = {item['pci_address'].lower(): item for item in inventory['pci_network_devices']}
    interfaces = {item['name']: item for item in inventory['interfaces']}
    return {
        'host': request.get('host'),
        'roles': request.get('roles', []),
        'configured': configured,
        'matched_receive_devices': [
            {'pci_address': address, 'observed': devices.get(address.lower())}
            for address in receive_addresses
        ],
        'matched_send_interfaces': [interfaces.get(name) for name in send_interfaces],
        'interfaces': inventory['interfaces'],
        'pci_network_devices': inventory['pci_network_devices'],
        'warnings': inventory['warnings'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', help='JSON object with host, roles and configured network settings')
    arguments = parser.parse_args()
    try:
        inventory = collect()
        if arguments.report is not None:
            inventory = build_report(inventory, json.loads(arguments.report))
        print(json.dumps(inventory, ensure_ascii=False))
    except (OSError, ValueError) as error:
        print('Network inventory failed: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
