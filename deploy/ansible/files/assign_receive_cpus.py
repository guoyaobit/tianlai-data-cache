#!/usr/bin/env python3
"""Assign receive devices and storage-send lanes to NUMA-local physical CPUs."""

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


def device_numa_node(root, address):
    if not PCI_ADDRESS.fullmatch(address):
        raise ValueError(f'invalid PCI address: {address}')
    value = read_text(root / 'bus/pci/devices' / address / 'numa_node')
    try:
        node = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'NUMA node is unavailable for {address}') from error
    if node < 0:
        raise ValueError(f'NUMA node is unknown for {address}')
    return node


def assign(sysfs, pci_addresses, reserve_cores=1, send_pci=None, send_core_count=0):
    """Assign receive devices and optional send lanes to distinct physical cores."""
    if reserve_cores < 0:
        raise ValueError('reserve_cores must not be negative')
    if send_core_count < 0 or (send_core_count and send_pci is None):
        raise ValueError('send core assignment requires a send PCI device')
    root = Path(sysfs)
    device_nodes = []
    for address in pci_addresses:
        device_nodes.append(device_numa_node(root, address))

    send_node = device_numa_node(root, send_pci) if send_pci is not None else None
    nodes = set(device_nodes)
    if send_node is not None:
        nodes.add(send_node)
    physical_by_node = {node: physical_cpus(root, node) for node in nodes}
    for node, candidates in physical_by_node.items():
        required = reserve_cores + device_nodes.count(node)
        if node == send_node:
            required += send_core_count
        if len(candidates) < required:
            raise ValueError(
                f'NUMA node {node} needs {required} physical CPUs but has {len(candidates)}')

    send_cpu_ids = []
    if send_node is not None:
        candidates = physical_by_node[send_node]
        send_cpu_ids = candidates[reserve_cores:reserve_cores + send_core_count]

    by_node = {}
    for node in set(device_nodes):
        offset = reserve_cores + (send_core_count if node == send_node else 0)
        by_node[node] = physical_by_node[node][offset:]

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
    return {
        'cpu_ids': [item['cpu_id'] for item in assignments],
        'send_cpu_ids': send_cpu_ids,
        'send_numa_node': send_node,
        'assignments': assignments,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sysfs', default='/sys')
    parser.add_argument('--reserve-cores-per-node', type=int, default=1)
    parser.add_argument('--send-pci')
    parser.add_argument('--send-core-count', type=int, default=0)
    parser.add_argument('pci_addresses', nargs='+')
    arguments = parser.parse_args()
    try:
        report = assign(arguments.sysfs, arguments.pci_addresses,
                        arguments.reserve_cores_per_node,
                        arguments.send_pci, arguments.send_core_count)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
