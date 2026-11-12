#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${TIANLAI_DISPLAY_USER:-ubuntu}@${TIANLAI_DISPLAY_HOST:-172.0.0.61}"

echo "Installing tlcache on ${target}"
echo "SSH may prompt for the display-node password; it is never stored by this script."
ssh "${target}" 'mkdir -p "$HOME/.local/bin"'
scp -q "${script_dir}/cache_status.py" "${target}:.local/bin/tlcache"
ssh "${target}" 'chmod 0755 "$HOME/.local/bin/tlcache"'

echo "Installed ~/.local/bin/tlcache on ${target}. Reconnect SSH if ~/.local/bin is not in PATH."
