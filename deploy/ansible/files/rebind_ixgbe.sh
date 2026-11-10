#!/usr/bin/env bash
set -euo pipefail

service_result="${1:-unknown}"
shift || true

# A failed cache process may be restarted by systemd; do not change its devices mid-restart.
if [[ "$service_result" != success ]]; then
    echo "Skip ixgbe rebind after cache result: $service_result" >&2
    exit 0
fi

modprobe ixgbe
failed=0
for pci in "$@"; do
    if [[ ! "$pci" =~ ^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]$ ]]; then
        echo "Invalid PCI address: $pci" >&2
        failed=1
        continue
    fi
    device="/sys/bus/pci/devices/$pci"
    if [[ ! -d "$device" ]]; then
        echo "PCI device not found: $pci" >&2
        failed=1
        continue
    fi
    if [[ -L "$device/driver" ]]; then
        driver=$(readlink -f "$device/driver")
        printf '%s' "$pci" > "$driver/unbind"
    fi
    if [[ -w "$device/driver_override" ]]; then
        printf '\n' > "$device/driver_override"
    fi
    if ! printf '%s' "$pci" > /sys/bus/pci/drivers/ixgbe/bind; then
        echo "Failed to bind $pci to ixgbe" >&2
        failed=1
        continue
    fi
    echo "Bound $pci to ixgbe"
done
exit "$failed"
