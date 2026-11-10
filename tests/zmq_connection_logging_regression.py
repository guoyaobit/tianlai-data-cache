#!/usr/bin/env python3
"""Verify that ZeroMQ connection progress is visible without noisy per-block logs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "Common/src/zmq_store.cpp").read_text(encoding="utf-8")
HEADER = (ROOT / "Common/include/zmq_store.h").read_text(encoding="utf-8")

assert '" connecting: source="' in SOURCE
assert '" connecting: interface="' not in SOURCE
assert '" connecting: source="' in SOURCE and '" destination="' in SOURCE
assert '" asynchronous connect started: destination="' in SOURCE
assert '" connected: destination="' in SOURCE
assert "if (!connectionReadyLogged)" in SOURCE
assert "bool connectionReadyLogged = false" in HEADER
assert SOURCE.count('" connected: destination="') == 1
print("ZeroMQ connection logging checks passed")
