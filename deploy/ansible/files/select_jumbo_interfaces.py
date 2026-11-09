#!/usr/bin/env python3
"""List physical Linux interfaces whose reported speed exceeds 1 Gb/s."""

import argparse
import json
from pathlib import Path


def read_number(path):
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, TypeError, ValueError):
        return None


def select_interfaces(sysfs_root, minimum_speed):
    """Return physical interfaces at or above the configured speed."""
    selected = []
    root = Path(sysfs_root)
    for interface in sorted(root.iterdir()) if root.is_dir() else []:
        if interface.name == 'lo' or not (interface / 'device').exists():
            continue
        speed = read_number(interface / 'speed')
        if speed is not None and speed >= minimum_speed:
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
    selected = select_interfaces(arguments.sysfs_root, arguments.minimum_speed)
    print(json.dumps({'minimum_speed_mbps': arguments.minimum_speed,
                      'interfaces': selected}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
