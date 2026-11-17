#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ $# -eq 1 && $1 =~ ^([0-9]|1[0-3])$ ]] || {
    echo "Usage: sudo tianlai-release-memory SERVER_ID(0-13)" >&2
    exit 2
}
if systemctl is-active --quiet tianlai-cache.service; then
    echo "Stop tianlai-cache.service before releasing its memory" >&2
    exit 1
fi
shopt -s nullglob
files=(/run/tianlai/server-"$1"-port-*.bin)
for file in "${files[@]}"; do
    exec {cache_fd}<>"$file"
    if ! flock -n "$cache_fd"; then
        echo "Shared cache is still in use: $file" >&2
        exit 1
    fi
    rm -- "$file"
    flock -u "$cache_fd"
    exec {cache_fd}>&-
    echo "Released $file"
done
if (( ${#files[@]} == 0 )); then
    echo "No shared cache files for server $1"
fi
