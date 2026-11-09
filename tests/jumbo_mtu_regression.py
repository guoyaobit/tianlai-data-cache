#!/usr/bin/env python3
"""Verify that Netplan jumbo MTU selection targets high-speed physical interfaces."""

import importlib.util
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/ansible/files/select_jumbo_interfaces.py'
spec = importlib.util.spec_from_file_location('select_jumbo_interfaces', SCRIPT)
jumbo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jumbo)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding='utf-8')


def main():
    with tempfile.TemporaryDirectory(prefix='tianlai-jumbo-') as directory:
        root = Path(directory)
        for name, speed, mtu, physical in [
                ('eth1', 1000, 1500, True),
                ('eth10', 10000, 1500, True),
                ('eth100', 100000, 9000, True),
                ('unknown0', -1, 1500, True),
                ('veth0', 10000, 1500, False),
                ('lo', 10000, 65536, False)]:
            interface = root / name
            interface.mkdir()
            write(interface / 'speed', speed)
            write(interface / 'mtu', mtu)
            if physical:
                (interface / 'device').mkdir()
        report = jumbo.select_interfaces(root, 1001)
        assert [item['name'] for item in report] == ['eth10', 'eth100']
        assert report[0]['current_mtu'] == 1500
        assert report[1]['current_mtu'] == 9000
    receive_template = (ROOT / 'deploy/ansible/templates/tianlai-receive.netplan.yaml.j2').read_text(
        encoding='utf-8')
    send_template = (ROOT / 'deploy/ansible/templates/tianlai-send.netplan.yaml.j2').read_text(
        encoding='utf-8')
    assert 'mtu: {{ tianlai_jumbo_mtu }}' in receive_template
    assert 'tianlai_send_ip' not in receive_template
    assert '"{{ tianlai_send_ip }}/{{ tianlai_cache_send_prefix_length }}"' in send_template
    assert 'dhcp4: false' in send_template and 'dhcp6: false' in send_template
    assert 'optional: true' in send_template
    inventory = (ROOT / 'deploy/ansible/inventory_example.yml').read_text(encoding='utf-8')
    for server_id in range(14):
        assert f'tianlai_send_ip: "192.168.210.{100 + server_id}"' in inventory
    print('Netplan network: cache data IP and high-speed MTU selection passed')


if __name__ == '__main__':
    main()
