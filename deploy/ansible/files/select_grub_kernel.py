"""Select an installed Ubuntu kernel by its stable GRUB menu ID."""

from pathlib import Path
import re
import subprocess
import sys


def main(series):
    if not re.fullmatch(r"[0-9]+\.[0-9]+", series):
        raise ValueError("kernel series must be major.minor")
    menu = Path("/boot/grub/grub.cfg").read_text(encoding="utf-8")
    ids = re.findall(r"(?m)^\s*(?:submenu|menuentry)\s+[^\n]*?\$menuentry_id_option\s+'([^']+)'", menu)
    match_kernel = re.compile(rf"gnulinux-({re.escape(series)}\.[a-zA-Z0-9.+-]+)-advanced-([a-zA-Z0-9-]+)")
    candidates = {}
    for entry in ids:
        match = match_kernel.fullmatch(entry)
        if not match:
            continue
        release, disk_id = match.groups()
        submenu = f"gnulinux-advanced-{disk_id}"
        if (submenu in ids and Path(f"/boot/vmlinuz-{release}").is_file()
                and Path(f"/boot/initrd.img-{release}").is_file()):
            candidates[release] = f"{submenu}>{entry}"
    if not candidates:
        print(f"no installed Linux {series}.x GRUB entry with kernel and initrd", file=sys.stderr)
        sys.exit(2)
    ordered = subprocess.run(
        ["sort", "-V"], input="\n".join(candidates) + "\n",
        text=True, capture_output=True, check=True).stdout.splitlines()
    print(candidates[ordered[-1]])


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
