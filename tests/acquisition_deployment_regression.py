#!/usr/bin/env python3
"""Static checks for the two-phase deploy/acquisition workflow."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy" / "ansible"

site = (ANSIBLE / "site.yml").read_text(encoding="utf-8")
configure = (ANSIBLE / "configure_acquisition.yml").read_text(encoding="utf-8")
settings = (ANSIBLE / "group_vars" / "all" / "settings.yml").read_text(encoding="utf-8")
inventory_yml = (ANSIBLE / "inventory_example.yml").read_text(encoding="utf-8")
inventory_ini = (ANSIBLE / "inventory_example.ini").read_text(encoding="utf-8")
recv_template = (ANSIBLE / "templates" / "recv.yaml.j2").read_text(encoding="utf-8")
controller_template = (ANSIBLE / "templates" / "controller.service.j2").read_text(encoding="utf-8")
deploy_script = (ANSIBLE / "deploy.sh").read_text(encoding="utf-8")
configure_script = (ANSIBLE / "configure_acquisition.sh").read_text(encoding="utf-8")
permissions = (ANSIBLE / "tasks" / "install_permissions.yml").read_text(encoding="utf-8")

assert "tianlai_acquisition_id is match" not in site
assert 'hosts: "cache_nodes:storage:controller"' in site
assert "hosts: localhost" not in site
assert site.count("run_once: true") >= 4
assert site.index("Install storage receiver") < site.index(
    "Generate per-server network reports after code deployment"
)
assert site.index("Build and install cache nodes") < site.index(
    "Generate per-server network reports after code deployment"
)
assert site.index("Install trigger controller") < site.index(
    "Generate per-server network reports after code deployment"
)
assert site.index("Copy shared sources") < site.index(
    "Require the ixcap SDK for the production build"
)
assert "Search common locations for the ixcap header" in site
assert "Search common locations for the ixcap library" in site
assert "tianlai_ixcap_include_effective" in site
assert "enabled: false\n        state: stopped" in site
assert "tianlai_acquisition_id" not in settings
assert "tianlai_acquisition_id" not in inventory_yml
assert "tianlai_acquisition_id" not in inventory_ini
assert 'tianlai_controller_ip: "192.168.200.250"' in inventory_yml
assert 'tianlai_backend_ip: "192.168.200.10"' in inventory_yml
assert 'tianlai_storage_directory: /data/raw-store' in inventory_yml
assert "tianlai_controller_ip: 192.168.200.250" in settings
assert "tianlai_backend_ip: 192.168.200.10" in settings
assert "ansible_ssh_private_key_file" not in settings
assert "ansible_ssh_private_key_file" not in site
assert "TIANLAI_SSH_PRIVATE_KEY" not in deploy_script
assert "TIANLAI_SSH_PRIVATE_KEY" not in configure_script
assert site.count("tasks/install_permissions.yml") == 4
assert 'owner: root' in permissions
assert 'group: "{{ ansible_user }}"' in permissions
assert "mode: 'u=rwX,g=rX,o='" in permissions
assert "default('UNCONFIGURED')" in recv_template
assert "default('UNCONFIGURED')" in controller_template
assert configure.index("Stop the controller before changing acquisition") < configure.index(
    "Configure and start cache nodes for an acquisition"
)
assert configure.index("Configure and start cache nodes for an acquisition") < configure.index(
    "Configure and start trigger controller for an acquisition"
)
assert configure.count("enabled: true") == 2
assert configure.count("state: restarted") == 2

print("acquisition deployment regression passed")
