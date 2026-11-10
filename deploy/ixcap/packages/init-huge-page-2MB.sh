#!/bin/bash
set -euo pipefail

HUGEPAGE_MOUNT=${HUGEPAGE_MOUNT:-/mnt/huge}
HUGEPAGE_SIZE=${HUGEPAGE_SIZE:-2M}
HUGEPAGE_SYSFS_NAME=${HUGEPAGE_SYSFS_NAME:-hugepages-2048kB}
HUGEPAGES_PER_NODE=${HUGEPAGES_PER_NODE:-5120}

mkdir -p "$HUGEPAGE_MOUNT"

if mountpoint -q "$HUGEPAGE_MOUNT"; then
	MOUNT_OPTS=$(findmnt -n -o OPTIONS --target "$HUGEPAGE_MOUNT" || true)

	if ! echo "$MOUNT_OPTS" | grep -Eq 'pagesize=(2M|2048K|2048k|2097152)'; then
		echo "ERROR: $HUGEPAGE_MOUNT is already mounted, but not with pagesize=2M." >&2
		echo "Current options: $MOUNT_OPTS" >&2
		echo "Please run:" >&2
		echo "  sudo umount $HUGEPAGE_MOUNT" >&2
		echo "  sudo mount -t hugetlbfs -o pagesize=2M hugetlbfs $HUGEPAGE_MOUNT" >&2
		exit 1
	fi
else
	mount -t hugetlbfs -o pagesize="$HUGEPAGE_SIZE" hugetlbfs "$HUGEPAGE_MOUNT"
fi

for node_dir in /sys/devices/system/node/node*
do
	[ -d "$node_dir" ] || continue

	HUGEPAGE_NODE_DIR="$node_dir/hugepages/$HUGEPAGE_SYSFS_NAME"

	if [ -e "$HUGEPAGE_NODE_DIR/nr_hugepages" ]; then
		echo "$HUGEPAGES_PER_NODE" > "$HUGEPAGE_NODE_DIR/nr_hugepages"
	fi
done

echo "2MB hugepage setup complete."
echo "Mount point: $HUGEPAGE_MOUNT"
echo "Free 2MB hugepages:"
cat /sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages
