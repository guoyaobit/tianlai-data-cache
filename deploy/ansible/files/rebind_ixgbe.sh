#!/usr/bin/env bash
set -uo pipefail

service_result="${1:-unknown}"
shift || true

echo "Restore receive devices to ixgbe after cache result: $service_result"

if ! modprobe ixgbe; then
    echo "Failed to load ixgbe" >&2
    exit 1
fi
if [[ ! -e /sys/bus/pci/drivers/ixgbe/bind ]]; then
    echo "ixgbe bind interface is unavailable" >&2
    exit 1
fi

failed=0
declare -a devices=()
declare -A seen=()
for argument in "$@"; do
    pci="${argument,,}"
    if [[ ! "$pci" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]]; then
        echo "Invalid PCI address: $argument" >&2
        failed=1
        continue
    fi
    if [[ -n "${seen[$pci]:-}" ]]; then
        echo "Duplicate PCI address: $pci" >&2
        failed=1
        continue
    fi
    seen[$pci]=1
    if [[ ! -d "/sys/bus/pci/devices/$pci" ]]; then
        echo "PCI device not found: $pci" >&2
        failed=1
        continue
    fi
    devices+=("$pci")
done

driver_name() {
    local driver="/sys/bus/pci/devices/$1/driver"
    [[ -L "$driver" ]] && basename "$(readlink -f "$driver")"
}

# Release every receive function first. Binding both functions of a card in the
# same retry round avoids leaving one port behind while the card is resetting.
for pci in "${devices[@]}"; do
    device="/sys/bus/pci/devices/$pci"
    current="$(driver_name "$pci")"
    if [[ -n "$current" && "$current" != ixgbe ]]; then
        if ! printf '%s' "$pci" > "$device/driver/unbind"; then
            echo "Initial unbind failed for $pci from $current; retrying" >&2
        fi
    fi
    if [[ -w "$device/driver_override" ]]; then
        if ! printf '%s' ixgbe > "$device/driver_override"; then
            echo "Failed to select ixgbe for $pci" >&2
            failed=1
        fi
    fi
done

declare -a pending=("${devices[@]}")
for attempt in {1..10}; do
    ((${#pending[@]} == 0)) && break
    for pci in "${pending[@]}"; do
        device="/sys/bus/pci/devices/$pci"
        current="$(driver_name "$pci")"
        if [[ -n "$current" && "$current" != ixgbe ]]; then
            printf '%s' "$pci" > "$device/driver/unbind" 2>/dev/null || true
        fi
        if [[ "$(driver_name "$pci")" != ixgbe ]]; then
            printf '%s' "$pci" > /sys/bus/pci/drivers/ixgbe/bind 2>/dev/null ||
                printf '%s' "$pci" > /sys/bus/pci/drivers_probe 2>/dev/null || true
        fi
    done
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=2 || true
    declare -a next_pending=()
    for pci in "${pending[@]}"; do
        [[ "$(driver_name "$pci")" == ixgbe ]] || next_pending+=("$pci")
    done
    pending=("${next_pending[@]}")
    ((${#pending[@]} == 0)) ||
        echo "ixgbe rebind attempt $attempt pending=${pending[*]}" >&2
    ((${#pending[@]} == 0)) || sleep 0.5
done

for pci in "${devices[@]}"; do
    device="/sys/bus/pci/devices/$pci"
    if [[ -w "$device/driver_override" ]]; then
        printf '\n' > "$device/driver_override" || true
    fi
    current="$(driver_name "$pci")"
    if [[ "$current" == ixgbe ]]; then
        interfaces=()
        while IFS= read -r interface; do interfaces+=("$(basename "$interface")"); done < <(
            find "$device/net" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort
        )
        echo "Bound $pci to ixgbe interfaces=${interfaces[*]:-pending-udev}"
    else
        echo "Failed to bind $pci to ixgbe; current_driver=${current:-none}" >&2
        failed=1
    fi
done

exit "$failed"
