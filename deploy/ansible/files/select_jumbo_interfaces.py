#!/usr/bin/env python3
"""List high-speed physical interfaces and stale link-local data ports."""

import argparse
import ipaddress
import json
from pathlib import Path
import subprocess


def read_number(path):
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, TypeError, ValueError):
        return None


def ipv4_link_local_interfaces():
    """Return interfaces carrying an IPv4 address in 169.254.0.0/16."""
    try:
        result = subprocess.run(
            ['ip', '-j', '-4', 'address', 'show'], check=True,
            capture_output=True, text=True, timeout=10)
        report = json.loads(result.stdout)
    except (FileNotFoundError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, TypeError, ValueError):
        return set()
    selected = set()
    for interface in report:
        name = interface.get('ifname')
        for address in interface.get('addr_info', []):
            try:
                value = ipaddress.ip_address(address.get('local', ''))
            except ValueError:
                continue
            if value.version == 4 and value.is_link_local and isinstance(name, str):
                selected.add(name)
    return selected


def select_interfaces(sysfs_root, minimum_speed, link_local_interfaces=()):
    """Return high-speed ports plus link-down physical ports with stale IPv4LL."""
    selected = []
    root = Path(sysfs_root)
    link_local = set(link_local_interfaces)
    for interface in sorted(root.iterdir()) if root.is_dir() else []:
        if interface.name == 'lo' or not (interface / 'device').exists():
            continue
        speed = read_number(interface / 'speed')
        speed_unknown = speed is None or speed < 0
        if (speed is not None and speed >= minimum_speed) or (
                speed_unknown and interface.name in link_local):
            selected.append({
                'name': interface.name,
                'speed_mbps': speed,
                'current_mtu': read_number(interface / 'mtu'),
            })
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minimum-speed', type=int, default=1001, metavar='MBPS')
    parser.add_argument('--sysfs-root', default='/sys/class/net')
    arguments = parser.parse_args()
    if arguments.minimum_speed <= 1000:
        parser.error('minimum speed must be greater than 1000 Mb/s')
    link_local = ipv4_link_local_interfaces()
    selected = select_interfaces(arguments.sysfs_root, arguments.minimum_speed, link_local)
    print(json.dumps({'minimum_speed_mbps': arguments.minimum_speed,
                      'ipv4_link_local_interfaces': sorted(link_local),
                      'interfaces': selected}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
