#!/usr/bin/env python3
"""Static checks for the standalone ixcap installer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'deploy/ixcap/install_ixcap.sh').read_text(encoding='utf-8')
README = (ROOT / 'deploy/ixcap/README.md').read_text(encoding='utf-8')
WRAPPER = (ROOT / 'deploy/ansible/deploy_ixcap.sh').read_text(encoding='utf-8')
PLAYBOOK = (ROOT / 'deploy/ansible/install_ixcap.yml').read_text(encoding='utf-8')

for required in (
        'set -euo pipefail',
        "[[ $EUID -eq 0 ]]",
        'tar -tzf',
        '/usr/local/include/ixcap_lib.h',
        '/usr/local/lib/libixcap.a',
        '/etc/ixcap/${license_id^^}.lic',
        'ixcap-hugepages.service',
        'HUGEPAGES_PER_NODE=$pages_per_node',
        'systemctl daemon-reload'):
    assert required in SCRIPT

assert 'license.lic' in README
assert '/etc/ixcap/<MAC>.lic' in README
assert '--no-start' in SCRIPT
assert 'sdk_directory="$script_directory/packages"' in SCRIPT
assert 'expected at most one MAC-license archive under' in SCRIPT
assert 'Licenses: not installed' in SCRIPT
assert 'ubuntu22.04.1-20260701.zip' not in README
assert 'TIANLAI_IXCAP_HEADER' in WRAPPER
assert 'TIANLAI_IXCAP_LIBRARY' in WRAPPER
assert 'TIANLAI_IXCAP_HUGEPAGE_SCRIPT' in WRAPPER
assert "find \"$package_dir\" -maxdepth 4" in WRAPPER
assert 'unzip' not in WRAPPER
assert 'unzip' not in PLAYBOOK
assert 'sdk-package.zip' not in README
assert 'hosts: cache_nodes' in PLAYBOOK
assert 'Copy ixcap SDK header' in PLAYBOOK
assert 'Copy ixcap SDK library' in PLAYBOOK
assert 'Remove transferred ixcap installation files' in PLAYBOOK
print('standalone ixcap installer checks passed')
