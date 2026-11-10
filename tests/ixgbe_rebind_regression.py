#!/usr/bin/env python3
"""Static checks for safe per-device ixgbe restoration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "deploy/ansible"
SCRIPT = (ANSIBLE / "files/rebind_ixgbe.sh").read_text(encoding="utf-8")
SERVICE = (ANSIBLE / "templates/cache.service.j2").read_text(encoding="utf-8")
SITE = (ANSIBLE / "site.yml").read_text(encoding="utf-8")

assert "modprobe ixgbe" in SCRIPT
assert 'driver/unbind' in SCRIPT
assert '/sys/bus/pci/drivers/ixgbe/bind' in SCRIPT
assert 'service_result" != success' in SCRIPT
assert "modprobe -r" not in SCRIPT
assert "ExecStopPost=/usr/local/sbin/tianlai-rebind-ixgbe $SERVICE_RESULT" in SERVICE
assert "tianlai_receive_devices" in SERVICE
assert "Keep cache disabled and stopped by default" in SITE
assert "Restore receive NICs to ixgbe after deployment" in SITE
print("per-device ixgbe restoration passed")
