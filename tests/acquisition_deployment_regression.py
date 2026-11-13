#!/usr/bin/env python3
"""Static checks for fixed internal acquisition deployment."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy" / "ansible"

site = (ANSIBLE / "site.yml").read_text(encoding="utf-8")
settings = (ANSIBLE / "group_vars" / "all" / "settings.yml").read_text(encoding="utf-8")
inventory_yml = (ANSIBLE / "inventory_example.yml").read_text(encoding="utf-8")
inventory_ini = (ANSIBLE / "inventory_example.ini").read_text(encoding="utf-8")
recv_template = (ANSIBLE / "templates" / "recv.yaml.j2").read_text(encoding="utf-8")
controller_template = (ANSIBLE / "templates" / "controller.service.j2").read_text(encoding="utf-8")
deploy_script = (ANSIBLE / "deploy.sh").read_text(encoding="utf-8")
permissions = (ANSIBLE / "tasks" / "install_permissions.yml").read_text(encoding="utf-8")
display_vars = (ANSIBLE / "group_vars" / "display.yml").read_text(encoding="utf-8")

assert "tianlai_acquisition_id is match" not in site
assert "tianlai_controller_ip == hostvars[groups['storage'][0]].ansible_host" in site
assert 'hosts: "cache_nodes:storage:controller:display"' in site
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
assert "libboost-dev" in site
assert site.count("enabled: true\n        state: started") >= 2
assert "Keep cache disabled and stopped by default" in site
assert "enabled: false\n        state: stopped" in site
assert "Restore receive NICs to ixgbe after deployment" in site
assert "tianlai_acquisition_id" not in settings
assert "tianlai_acquisition_id" not in inventory_yml
assert "tianlai_acquisition_id" not in inventory_ini
assert 'tianlai_controller_ip: "172.0.0.142"' in inventory_yml
assert 'tianlai_backend_ip:' not in inventory_yml
assert 'tianlai_result_bind_ip: "0.0.0.0"' in inventory_yml
assert 'tianlaiview:' in inventory_yml and 'ansible_host: "172.0.0.61"' in inventory_yml
assert '[display]' in inventory_ini and 'tianlaiview ansible_host=172.0.0.61' in inventory_ini
assert "TIANLAI_DISPLAY_BECOME_PASSWORD" in display_vars
assert "ansible_become_password" in display_vars
assert "BECOME password for tianlaiview" in deploy_script
assert 'tianlai_storage_directory: /data' in inventory_yml
assert 'tianlai_storage_directory: /data' in settings
assert 'tianlai_storage_directory=/data' in inventory_ini
assert "tianlai_controller_ip: 172.0.0.142" in settings
assert "tianlai_backend_ip:" not in settings
assert "tianlai_result_bind_ip: 0.0.0.0" in settings
assert "ansible_ssh_private_key_file" not in settings
assert "ansible_ssh_private_key_file" not in site
assert "TIANLAI_SSH_PRIVATE_KEY" not in deploy_script
assert site.count("tasks/install_permissions.yml") == 5
assert 'owner: root' in permissions
assert 'group: "{{ ansible_user }}"' in permissions
assert "mode: 'u=rwX,g=rX,o='" in permissions
assert 'acquisitionId:' not in recv_template
assert 'acquisitionId = "tianlai"' in (ROOT / "Common/include/yaml_config.h").read_text(encoding="utf-8")
assert 'acquisitionId is fixed internally' in (ROOT / "Common/src/yaml_config.cpp").read_text(encoding="utf-8")
assert "--acquisition" not in controller_template
assert "--allow-source" not in controller_template
controller_source = (ROOT / "tools" / "controller.py").read_text(encoding="utf-8")
assert "peer[0] != allowed" not in controller_source
assert not (ANSIBLE / "configure_acquisition.sh").exists()
assert not (ANSIBLE / "configure_acquisition.yml").exists()

print("acquisition deployment regression passed")
