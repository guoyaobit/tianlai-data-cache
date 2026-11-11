#!/usr/bin/env python3
"""Assign each receive PCI device a physical CPU on its NUMA node."""

import argparse
import json
from pathlib import Path
import re


PCI_ADDRESS = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')


def read_text(path):
    try:
        return path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None


def parse_cpu_list(value):
    """Parse Linux cpulist syntax such as 0-3,8,10-11."""
    cpus = []
    for part in (value or '').split(','):
        if not part:
            continue
        bounds = part.split('-', 1)
        try:
            first = int(bounds[0])
            last = int(bounds[-1])
        except ValueError as error:
            raise ValueError(f'invalid CPU list: {value}') from error
        if first < 0 or last < first:
            raise ValueError(f'invalid CPU list: {value}')
        cpus.extend(range(first, last + 1))
    return cpus


def physical_cpus(sysfs, numa_node):
    """Return one online logical CPU for each physical core on a NUMA node."""
    root = Path(sysfs)
    node_cpus = parse_cpu_list(read_text(root / f'devices/system/node/node{numa_node}/cpulist'))
    online = set(parse_cpu_list(read_text(root / 'devices/system/cpu/online')))
    if online:
        node_cpus = [cpu for cpu in node_cpus if cpu in online]
    cores = {}
    for cpu in node_cpus:
        topology = root / f'devices/system/cpu/cpu{cpu}/topology'
        package = read_text(topology / 'physical_package_id')
        core = read_text(topology / 'core_id')
        if package is None or core is None:
            raise ValueError(f'CPU {cpu} topology is unavailable')
        cores.setdefault((package, core), cpu)
    return sorted(cores.values())


def assign(sysfs, pci_addresses, reserve_cores=1):
    """Assign ordered devices to unique local physical cores."""
    if reserve_cores < 0:
        raise ValueError('reserve_cores must not be negative')
    root = Path(sysfs)
    by_node = {}
    device_nodes = []
    for address in pci_addresses:
        if not PCI_ADDRESS.fullmatch(address):
            raise ValueError(f'invalid PCI address: {address}')
        value = read_text(root / 'bus/pci/devices' / address / 'numa_node')
        try:
            node = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f'NUMA node is unavailable for {address}') from error
        if node < 0:
            raise ValueError(f'NUMA node is unknown for {address}')
        device_nodes.append(node)
        if node not in by_node:
            candidates = physical_cpus(root, node)
            if len(candidates) <= reserve_cores:
                raise ValueError(f'NUMA node {node} has no assignable physical CPU')
            by_node[node] = candidates[reserve_cores:]

    used = {node: 0 for node in by_node}
    assignments = []
    for port, (address, node) in enumerate(zip(pci_addresses, device_nodes)):
        index = used[node]
        if index >= len(by_node[node]):
            raise ValueError(f'NUMA node {node} lacks physical CPUs for receive ports')
        cpu = by_node[node][index]
        used[node] += 1
        assignments.append({
            'port': port,
            'pci_address': address,
            'numa_node': node,
            'cpu_id': cpu,
        })
    return {'cpu_ids': [item['cpu_id'] for item in assignments], 'assignments': assignments}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysfs', default='/sys')
    parser.add_argument('--reserve-cores-per-node', type=int, default=1)
    parser.add_argument('pci_addresses', nargs='+')
    arguments = parser.parse_args()
    try:
        report = assign(arguments.sysfs, arguments.pci_addresses,
                        arguments.reserve_cores_per_node)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
