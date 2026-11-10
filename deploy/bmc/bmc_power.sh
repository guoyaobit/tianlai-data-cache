#!/usr/bin/env bash
set -uo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
host_file="${TIANLAI_BMC_HOSTS:-$script_dir/bmc_hosts.conf}"
bmc_user="${BMC_USER:-}"
assume_yes=false

usage() {
    cat <<'EOF'
Usage: bmc_power.sh [--user USER] [--yes] ACTION TARGET...

Actions:
  status       Read current power state.
  on           Power on.
  off          Request an ACPI soft shutdown.
  force-off    Cut power immediately.
  cycle        Power cycle immediately.

Targets:
  compute      tianlai1 through tianlai4.
  cache        tianlaistore1 through tianlaistore14.
  storage      tianlairaid.
  all          Every host in bmc_hosts.conf.
  HOSTNAME     One or more explicit hostnames from bmc_hosts.conf.

Credentials are read from BMC_USER and IPMI_PASSWORD, or prompted securely.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            [[ $# -ge 2 ]] || { echo "--user requires a value" >&2; exit 2; }
            bmc_user=$2
            shift 2
            ;;
        --yes)
            assume_yes=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

[[ $# -ge 2 ]] || { usage >&2; exit 2; }
action=$1
shift
case "$action" in
    status) ipmi_action=(status) ;;
    on) ipmi_action=(on) ;;
    off) ipmi_action=(soft) ;;
    force-off) ipmi_action=(off) ;;
    cycle) ipmi_action=(cycle) ;;
    *) echo "Unknown action: $action" >&2; usage >&2; exit 2 ;;
esac

command -v ipmitool >/dev/null 2>&1 || {
    echo "ipmitool is required. Install it with: sudo apt install ipmitool" >&2
    exit 1
}
[[ -r "$host_file" ]] || { echo "BMC host file is not readable: $host_file" >&2; exit 1; }

declare -A host_ips host_groups selected
host_order=()
while read -r name address group extra; do
    [[ -z "${name:-}" || "$name" == \#* ]] && continue
    if [[ -n "${extra:-}" || ! "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || -z "$group" ]]; then
        echo "Invalid BMC host entry: $name $address $group ${extra:-}" >&2
        exit 1
    fi
    [[ -z "${host_ips[$name]+set}" ]] || { echo "Duplicate BMC hostname: $name" >&2; exit 1; }
    host_ips[$name]=$address
    host_groups[$name]=$group
    host_order+=("$name")
done < "$host_file"

for target in "$@"; do
    matched=false
    if [[ "$target" == all ]]; then
        for name in "${host_order[@]}"; do selected[$name]=1; done
        matched=true
    elif [[ "$target" == compute || "$target" == cache || "$target" == storage ]]; then
        for name in "${host_order[@]}"; do
            if [[ "${host_groups[$name]}" == "$target" ]]; then
                selected[$name]=1
                matched=true
            fi
        done
    elif [[ -n "${host_ips[$target]+set}" ]]; then
        selected[$target]=1
        matched=true
    fi
    $matched || { echo "Unknown BMC target: $target" >&2; exit 2; }
done

targets=()
for name in "${host_order[@]}"; do
    [[ -n "${selected[$name]+set}" ]] && targets+=("$name")
done
[[ ${#targets[@]} -gt 0 ]] || { echo "No BMC targets selected" >&2; exit 2; }

printf 'Action: %s\nTargets:\n' "$action"
for name in "${targets[@]}"; do printf '  %-16s %s\n' "$name" "${host_ips[$name]}"; done

if [[ "$action" != status && "$assume_yes" != true ]]; then
    printf 'Type %s to continue: ' "$action"
    read -r confirmation
    [[ "$confirmation" == "$action" ]] || { echo "Cancelled."; exit 1; }
fi

if [[ -z "$bmc_user" ]]; then
    read -r -p 'BMC username: ' bmc_user
fi
[[ -n "$bmc_user" ]] || { echo "BMC username cannot be empty" >&2; exit 2; }
if [[ -z "${IPMI_PASSWORD:-}" ]]; then
    read -r -s -p 'BMC password: ' IPMI_PASSWORD
    echo
    export IPMI_PASSWORD
fi
[[ -n "${IPMI_PASSWORD:-}" ]] || { echo "BMC password cannot be empty" >&2; exit 2; }

failed=0
for name in "${targets[@]}"; do
    printf '\n[%s %s] %s\n' "$name" "${host_ips[$name]}" "$action"
    if ! timeout --foreground "${BMC_TIMEOUT_SECONDS:-20}" \
        ipmitool -I lanplus -H "${host_ips[$name]}" -U "$bmc_user" -E \
        chassis power "${ipmi_action[@]}"; then
        echo "BMC operation failed: $name" >&2
        failed=$((failed + 1))
    fi
done
unset IPMI_PASSWORD

if (( failed > 0 )); then
    echo "$failed of ${#targets[@]} BMC operations failed" >&2
    exit 1
fi
echo "Completed ${#targets[@]} BMC operation(s)."
