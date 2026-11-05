#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
inventory="${TIANLAI_INVENTORY:-inventory.yml}"
private_key="${TIANLAI_SSH_PRIVATE_KEY:-$HOME/.ssh/tianlai_ansible}"

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
if [[ ! -r "$private_key" ]]; then
    echo "SSH private key is not readable: $private_key" >&2
    exit 2
fi

ansible-inventory -i "$inventory" --list >/dev/null
ansible-playbook -i "$inventory" --syntax-check site.yml
ansible-playbook -i "$inventory" --ask-become-pass site.yml "$@"
