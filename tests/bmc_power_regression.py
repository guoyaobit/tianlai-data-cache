#!/usr/bin/env python3
"""Static checks for safe BMC power control."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BMC = ROOT / "deploy/bmc"
SCRIPT = (BMC / "bmc_power.sh").read_text(encoding="utf-8")
HOSTS = (BMC / "bmc_hosts.conf").read_text(encoding="utf-8").splitlines()

entries = [line.split() for line in HOSTS if line and not line.startswith("#")]
assert len(entries) == 19
assert entries[0] == ["tianlai1", "172.0.0.200", "compute"]
assert entries[4] == ["tianlaistore1", "172.0.0.204", "cache"]
assert entries[17] == ["tianlaistore14", "172.0.0.217", "cache"]
assert entries[18] == ["tianlairaid", "172.0.0.218", "storage"]
assert "-I lanplus" in SCRIPT and "-E" in SCRIPT
assert "IPMI_PASSWORD" in SCRIPT and "--password" not in SCRIPT
assert "Type %s to continue" in SCRIPT
assert "force-off) ipmi_action=(off)" in SCRIPT
assert "off) ipmi_action=(soft)" in SCRIPT
assert "--yes" in SCRIPT and "timeout --foreground" in SCRIPT
print("BMC power control checks passed")
