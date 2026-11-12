#!/usr/bin/env python3
"""Static checks for network diagnostic package deployment."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy/ansible"
PLAYBOOK = (ANSIBLE / "install_network_tools.yml").read_text(encoding="utf-8")
TASKS = (ANSIBLE / "tasks/network_tools.yml").read_text(encoding="utf-8")
SITE = (ANSIBLE / "site.yml").read_text(encoding="utf-8")

assert 'hosts: "cache_nodes:storage:controller:display"' in PLAYBOOK
assert "tasks/network_tools.yml" in PLAYBOOK
assert "Install network diagnostics on all nodes" in SITE
assert "tasks/network_tools.yml" in SITE

for package in (
    "bwm-ng",
    "ethtool",
    "iftop",
    "iperf3",
    "iproute2",
    "iputils-ping",
    "mtr-tiny",
    "netcat-openbsd",
    "nload",
    "nmap",
    "socat",
    "tcpdump",
    "tshark",
    "traceroute",
    "wireshark",
    "wireshark-common",
):
    assert f"- {package}" in TASKS

assert "update_cache_retries: 5" in TASKS
assert "state: latest" in TASKS
assert "wireshark-common/install-setuid" in TASKS
assert "value: 'false'" in TASKS
print("network diagnostic package deployment passed")
