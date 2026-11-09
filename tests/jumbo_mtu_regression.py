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
    storage_template = (ROOT / 'deploy/ansible/templates/tianlai-storage.netplan.yaml.j2').read_text(
        encoding='utf-8')
    assert 'mtu: {{ tianlai_jumbo_mtu }}' in receive_template
    assert 'tianlai_send_ip' not in receive_template
    assert '"{{ tianlai_send_ip }}/{{ tianlai_cache_send_prefix_length }}"' in send_template
    assert 'dhcp4: false' in send_template and 'dhcp6: false' in send_template
    for template in (receive_template, send_template, storage_template):
        assert 'dhcp4: false' in template
        assert 'dhcp6: false' in template
        assert 'accept-ra: false' in template
        assert 'link-local: []' in template
    assert 'optional: true' in send_template
    assert '"{{ tianlai_storage_receive_interface }}"' in storage_template
    assert '"{{ tianlai_storage_ip }}/{{ tianlai_storage_prefix_length }}"' in storage_template
    settings = (ROOT / 'deploy/ansible/group_vars/all/settings.yml').read_text(encoding='utf-8')
    assert 'tianlai_storage_receive_interface: enp152s0f0np0' in settings
    network_tasks = (ROOT / 'deploy/ansible/tasks/network_config.yml').read_text(encoding='utf-8')
    assert "select('match', '^169[.]254[.]')" in network_tasks
    assert 'Require the configured storage IP only' in network_tasks
    assert 'Require no IPv6 addresses on managed data interfaces' in network_tasks
    inventory = (ROOT / 'deploy/ansible/inventory_example.yml').read_text(encoding='utf-8')
    for server_id in range(14):
        assert f'tianlai_send_ip: "192.168.210.{100 + server_id}"' in inventory
    print('Netplan network: cache data IP and high-speed MTU selection passed')


if __name__ == '__main__':
    main()
