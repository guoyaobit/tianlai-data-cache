#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
inventory="${TIANLAI_INVENTORY:-inventory.yml}"
acquisition_id="${1:-}"

if [[ ! "$acquisition_id" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || \
    [[ "$acquisition_id" == "CHANGE_ME" ]] || \
    [[ "$acquisition_id" == "UNCONFIGURED" ]]; then
    echo "Usage: bash configure_acquisition.sh ACQUISITION_ID [ansible options]" >&2
    echo "ACQUISITION_ID must contain 1-64 letters, numbers, underscores, or hyphens." >&2
    exit 2
fi
shift

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
ansible-playbook -i "$inventory" --syntax-check configure_acquisition.yml
ansible-playbook -i "$inventory" --ask-become-pass configure_acquisition.yml \
    --extra-vars "tianlai_acquisition_id=$acquisition_id" "$@"
