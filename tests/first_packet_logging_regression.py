#!/usr/bin/env python3
"""Verify that each receive thread logs its first accepted UDP packet once."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "Common/src/receive_antenna.cpp").read_text(encoding="utf-8")

assert "bool &firstPacketLogged" in SOURCE
assert SOURCE.count("bool firstPacketLogged = false") == 2
assert "first UDP packet: packet_id=" in SOURCE
for field in ("board=", "trace_period_id=", "integration_period_id=", "absolute_period="):
    assert field in SOURCE
assert "receive(absolutePeriod, slot, h.ifTrace, data) &&" in SOURCE
assert "!firstPacketLogged" in SOURCE
assert "firstPacketLogged = true" in SOURCE
print("first UDP packet logging checks passed")
