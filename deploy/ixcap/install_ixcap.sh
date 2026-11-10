#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  sudo bash install_ixcap.sh --sdk-dir SDK_DIRECTORY --licenses LICENSES.tar.gz [options]

Options:
  --pages-per-node N  Number of 2 MB hugepages per NUMA node (default: 5120).
  --no-start          Install and enable the service without allocating hugepages now.
  -h, --help          Show this help.
EOF
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

sdk_directory=''
license_archive=''
pages_per_node=5120
start_service=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sdk-dir)
            [[ $# -ge 2 ]] || fail '--sdk-dir requires a path'
            sdk_directory=$2
            shift 2
            ;;
        --licenses)
            [[ $# -ge 2 ]] || fail '--licenses requires a path'
            license_archive=$2
            shift 2
            ;;
        --pages-per-node)
            [[ $# -ge 2 ]] || fail '--pages-per-node requires a number'
            pages_per_node=$2
            shift 2
            ;;
        --no-start)
            start_service=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

[[ $EUID -eq 0 ]] || fail 'run this script with sudo'
[[ -n $sdk_directory && -d $sdk_directory ]] || fail 'SDK directory does not exist'
[[ -n $license_archive && -f $license_archive ]] || fail 'license archive does not exist'
[[ $pages_per_node =~ ^[0-9]+$ ]] || fail '--pages-per-node must be a non-negative integer'

for command in tar install find systemctl mountpoint findmnt readlink mktemp; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

sdk_directory=$(readlink -f "$sdk_directory")
license_archive=$(readlink -f "$license_archive")
work_dir=$(mktemp -d /tmp/tianlai-ixcap.XXXXXX)
trap 'rm -rf -- "$work_dir"' EXIT

# Reject license archive entries that could escape the temporary extraction directory.
while IFS= read -r entry; do
    case "$entry" in
        /*|../*|*/../*|*/..)
            fail "unsafe license archive entry: $entry"
            ;;
    esac
done < <(tar -tzf "$license_archive")

mkdir -p "$work_dir/licenses"
tar -xzf "$license_archive" -C "$work_dir/licenses"

mapfile -t headers < <(find "$sdk_directory" -type f -name ixcap_lib.h -print)
mapfile -t libraries < <(find "$sdk_directory" -type f -name libixcap.a -print)
mapfile -t hugepage_scripts < <(find "$sdk_directory" -type f -name init-huge-page-2MB.sh -print)
mapfile -t licenses < <(find "$work_dir/licenses" -type f -name '*.lic' -print | sort)

[[ ${#headers[@]} -eq 1 ]] || fail "expected one ixcap_lib.h, found ${#headers[@]}"
[[ ${#libraries[@]} -eq 1 ]] || fail "expected one libixcap.a, found ${#libraries[@]}"
[[ ${#hugepage_scripts[@]} -eq 1 ]] || fail \
    "expected one init-huge-page-2MB.sh, found ${#hugepage_scripts[@]}"
[[ ${#licenses[@]} -gt 0 ]] || fail 'no .lic files found in the license archive'

install -d -m 0755 /usr/local/include /usr/local/lib /usr/local/sbin
install -m 0644 "${headers[0]}" /usr/local/include/ixcap_lib.h
install -m 0644 "${libraries[0]}" /usr/local/lib/libixcap.a
install -m 0755 "${hugepage_scripts[0]}" /usr/local/sbin/ixcap-hugepages

# The current library looks up /etc/ixcap/<12-digit-MAC>.lic for each card.
install -d -m 0700 /etc/ixcap
for license in "${licenses[@]}"; do
    name=$(basename "$license")
    [[ $name =~ ^[0-9A-Fa-f]{12}\.lic$ ]] || fail "invalid license filename: $name"
    license_id=${name%.lic}
    install -m 0600 "$license" "/etc/ixcap/${license_id^^}.lic"
done

install -d -m 0755 /etc/default
printf '%s\n' \
    'HUGEPAGE_MOUNT=/mnt/huge' \
    'HUGEPAGE_SIZE=2M' \
    'HUGEPAGE_SYSFS_NAME=hugepages-2048kB' \
    "HUGEPAGES_PER_NODE=$pages_per_node" \
    > "$work_dir/ixcap-hugepages.default"
install -m 0644 "$work_dir/ixcap-hugepages.default" /etc/default/ixcap-hugepages

cat > "$work_dir/ixcap-hugepages.service" <<'EOF'
[Unit]
Description=Configure 2 MB hugepages for ixcap
After=local-fs.target
Before=tianlai-cache.service

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/ixcap-hugepages
ExecStart=/usr/local/sbin/ixcap-hugepages
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
install -m 0644 "$work_dir/ixcap-hugepages.service" \
    /etc/systemd/system/ixcap-hugepages.service

systemctl daemon-reload
systemctl enable ixcap-hugepages.service >/dev/null
if [[ $start_service == true ]]; then
    systemctl restart ixcap-hugepages.service
fi

echo 'ixcap SDK installation complete.'
echo 'Header: /usr/local/include/ixcap_lib.h'
echo 'Library: /usr/local/lib/libixcap.a'
echo "Licenses: ${#licenses[@]} file(s) in /etc/ixcap"
echo "Hugepages: $pages_per_node x 2 MB per NUMA node"
if [[ $start_service == false ]]; then
    echo 'Hugepage service was not started; start it with:'
    echo '  sudo systemctl start ixcap-hugepages.service'
fi
