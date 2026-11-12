#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ssh_user="${TIANLAI_SSH_USER:-ubuntu}"

if (( $# > 0 )); then
    hosts=("$@")
else
    hosts=("172.0.0.142")
fi

for host in "${hosts[@]}"; do
    target="${ssh_user}@${host}"
    echo "Installing tltrigger on ${target}"
    ssh "${target}" 'mkdir -p "$HOME/.local/bin"'
    scp -q "${script_dir}/trigger.py" "${target}:.local/bin/tltrigger"
    ssh "${target}" 'chmod 0755 "$HOME/.local/bin/tltrigger"'
done

echo "Installed tltrigger on ${#hosts[@]} nodes. Reconnect SSH if ~/.local/bin is not yet in PATH."
