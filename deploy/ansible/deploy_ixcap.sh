#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
inventory="${TIANLAI_INVENTORY:-inventory.yml}"
default_package_dir=$(readlink -m ../ixcap/packages)
package_dir="${TIANLAI_IXCAP_PACKAGE_DIR:-$default_package_dir}"

fail() {
    echo "ERROR: $*" >&2
    exit 2
}

for command in ansible-playbook ansible-inventory tar find readlink grep head; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
[[ -f "$inventory" ]] || fail "inventory not found: $inventory"
[[ -d "$package_dir" ]] || fail \
    "ixcap package directory not found: $package_dir (create it and copy the extracted SDK files and license archive there)"

header_matches=$(find "$package_dir" -maxdepth 4 -type f -name ixcap_lib.h -print)
library_matches=$(find "$package_dir" -maxdepth 4 -type f -name libixcap.a -print)
hugepage_matches=$(find "$package_dir" -maxdepth 4 -type f -name init-huge-page-2MB.sh -print)
header_count=$(printf '%s\n' "$header_matches" | grep -c . || true)
library_count=$(printf '%s\n' "$library_matches" | grep -c . || true)
hugepage_count=$(printf '%s\n' "$hugepage_matches" | grep -c . || true)
[[ $header_count -eq 1 ]] || fail \
    "expected exactly one ixcap_lib.h in $package_dir, found $header_count"
[[ $library_count -eq 1 ]] || fail \
    "expected exactly one libixcap.a in $package_dir, found $library_count"
[[ $hugepage_count -eq 1 ]] || fail \
    "expected exactly one init-huge-page-2MB.sh in $package_dir, found $hugepage_count"
sdk_header=$(printf '%s\n' "$header_matches" | head -n 1)
sdk_library=$(printf '%s\n' "$library_matches" | head -n 1)
sdk_hugepage_script=$(printf '%s\n' "$hugepage_matches" | head -n 1)

license_count=0
license_archive=''
archive_matches=$(find "$package_dir" -maxdepth 4 -type f \
    \( -name '*.tar.gz' -o -name '*.tgz' \) -print)
while IFS= read -r archive; do
    [[ -n "$archive" ]] || continue
    listing=$(tar -tzf "$archive" 2>/dev/null || true)
    if printf '%s\n' "$listing" | grep -qE '(^|/)[0-9A-Fa-f]{12}\.lic$'; then
        license_count=$((license_count + 1))
        license_archive=$(readlink -f "$archive")
    fi
done <<EOF
$archive_matches
EOF

[[ $license_count -eq 1 ]] || fail \
    "expected exactly one MAC-license archive in $package_dir, found $license_count"

pages_per_node="${TIANLAI_IXCAP_HUGEPAGES_PER_NODE:-5120}"
case "$pages_per_node" in
    ''|*[!0-9]*)
        fail 'TIANLAI_IXCAP_HUGEPAGES_PER_NODE must be a non-negative integer'
        ;;
esac

export TIANLAI_IXCAP_HEADER="$(readlink -f "$sdk_header")"
export TIANLAI_IXCAP_LIBRARY="$(readlink -f "$sdk_library")"
export TIANLAI_IXCAP_HUGEPAGE_SCRIPT="$(readlink -f "$sdk_hugepage_script")"
export TIANLAI_IXCAP_LICENSE_ARCHIVE="$license_archive"
export TIANLAI_IXCAP_HUGEPAGES_PER_NODE="$pages_per_node"

echo "SDK header: $TIANLAI_IXCAP_HEADER"
echo "SDK library: $TIANLAI_IXCAP_LIBRARY"
echo "Hugepage script: $TIANLAI_IXCAP_HUGEPAGE_SCRIPT"
echo "License package: $TIANLAI_IXCAP_LICENSE_ARCHIVE"
echo "Target group: cache_nodes"

ansible-inventory -i "$inventory" --list >/dev/null
ansible-playbook -i "$inventory" --syntax-check install_ixcap.yml
ansible-playbook -i "$inventory" --ask-become-pass install_ixcap.yml "$@"
