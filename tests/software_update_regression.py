#!/usr/bin/env python3
"""Static checks for source-only software updates."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy/ansible"
PLAYBOOK = (ANSIBLE / "update_software.yml").read_text(encoding="utf-8")
SCRIPT = (ANSIBLE / "update_software.sh").read_text(encoding="utf-8")
README = (ANSIBLE / "README.md").read_text(encoding="utf-8")

for host_group in ("hosts: storage", "hosts: cache_nodes", "hosts: controller"):
    assert host_group in PLAYBOOK

for preserved in (
    "storage_was_running",
    "cache_was_running",
    "controller_was_running",
    "Restore running storage receiver",
    "Restore running cache",
    "Restore running controller",
):
    assert preserved in PLAYBOOK

assert "templates/recv.yaml.j2" not in PLAYBOOK
assert "templates/server.yaml.j2" not in PLAYBOOK
assert "templates/storage.yaml.j2" not in PLAYBOOK
assert "templates/nodes.json.j2" not in PLAYBOOK
assert "network_config.yml" not in PLAYBOOK
assert "ansible.builtin.apt:" not in PLAYBOOK
assert "/usr/include/boost/property_tree/ptree.hpp" in PLAYBOOK
assert "install libboost-dev" in PLAYBOOK
assert "update_software.yml" in SCRIPT
assert "--syntax-check update_software.yml" in SCRIPT
assert "bash update_software.sh" in README
print("source-only software update deployment passed")
