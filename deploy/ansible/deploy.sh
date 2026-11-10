#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
inventory="${TIANLAI_INVENTORY:-inventory.yml}"

if ! command -v ansible-playbook >/dev/null 2>&1 || \
    ! command -v ansible-inventory >/dev/null 2>&1; then
    echo "Ansible playbook and inventory tools are required on the control host." >&2
    exit 2
fi
if [[ ! -f "$inventory" ]]; then
    echo "Inventory not found: $inventory" >&2
    echo "Copy inventory_example.yml to inventory.yml and complete it first." >&2
    exit 2
fi
ansible-inventory -i "$inventory" --list >/dev/null
ansible-playbook -i "$inventory" --syntax-check site.yml
ansible-playbook -i "$inventory" --ask-become-pass site.yml "$@"
