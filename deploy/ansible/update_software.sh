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
    exit 2
fi

ansible-inventory -i "$inventory" --list >/dev/null
ansible-playbook -i "$inventory" --syntax-check update_software.yml
echo "Parallel update enabled; build jobs are configured by tianlai_build_jobs."

# The display host uses a different sudo password. Keep it out of files and argv.
if [[ -z "${TIANLAI_DISPLAY_BECOME_PASSWORD:-}" ]]; then
    read -r -s -p "BECOME password for tianlaiview: " TIANLAI_DISPLAY_BECOME_PASSWORD
    echo
    export TIANLAI_DISPLAY_BECOME_PASSWORD
fi
ansible-playbook -i "$inventory" --ask-become-pass update_software.yml "$@"
