#!/usr/bin/env bash
set -euo pipefail

service_result="${1:-unknown}"
shift || true

echo "Restore receive devices to ixgbe after cache result: $service_result"

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
        if [[ "$(basename "$driver")" == ixgbe ]]; then
            echo "$pci is already bound to ixgbe"
            continue
        fi
        if ! printf '%s' "$pci" > "$driver/unbind"; then
            echo "Failed to unbind $pci from $(basename "$driver")" >&2
            failed=1
            continue
        fi
    fi
    if [[ -w "$device/driver_override" ]]; then
        if ! printf '\n' > "$device/driver_override"; then
            echo "Failed to clear driver override for $pci" >&2
            failed=1
            continue
        fi
    fi
    if ! printf '%s' "$pci" > /sys/bus/pci/drivers/ixgbe/bind; then
        echo "Failed to bind $pci to ixgbe" >&2
        failed=1
        continue
    fi
    echo "Bound $pci to ixgbe"
done
exit "$failed"
