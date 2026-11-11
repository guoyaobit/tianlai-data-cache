#!/usr/bin/env python3
"""Verify raw and accepted first-packet diagnostics for every receive thread."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "Common/src/receive_antenna.cpp").read_text(encoding="utf-8")

assert "bool &firstPacketLogged" in SOURCE
assert SOURCE.count("bool firstPacketLogged = false") == 2
assert SOURCE.count("bool firstUdpLogged = false") == 2
assert "bool firstUnparsedFrameLogged = false" in SOURCE
assert "first captured UDP packet: packet_id=" in SOURCE
assert "captured a frame but Ethernet/IPv4/UDP parsing failed" in SOURCE
assert "first accepted UDP packet: packet_id=" in SOURCE
for field in ("board=", "trace_period_id=", "integration_period_id=", "absolute_period="):
    assert field in SOURCE
assert "logFirstUdpPacket(p, data, bytes, frameBytes," in SOURCE
assert "receive(absolutePeriod, slot, h.ifTrace, data)" in SOURCE
assert "!firstPacketLogged" in SOURCE
assert "firstPacketLogged = true" in SOURCE
print("first UDP packet logging checks passed")
