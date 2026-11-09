#!/usr/bin/env python3
"""Check that simulation UDP ports are opt-in in production configuration."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
configs = sorted((ROOT / "F0-2").glob("server_*.yaml"))
configs += sorted((ROOT / "F3-10").glob("server_*.yaml"))
configs += sorted((ROOT / "F11-13").glob("server_*.yaml"))
assert len(configs) == 14

simulation_port = re.compile(r"^\s+#?\s*simulationPort\d+:", re.MULTILINE)
for path in configs:
    text = path.read_text(encoding="utf-8")
    assert not simulation_port.search(text), path

template = (ROOT / "deploy/ansible/templates/server.yaml.j2").read_text(encoding="utf-8")
assert "simulationPort" not in template

parser = (ROOT / "Common/src/yaml_config.cpp").read_text(encoding="utf-8")
assert "node[simulationKey] ? number(node, simulationKey, 1024, 65535) : 0" in parser

application = (ROOT / "Common/src/application.cpp").read_text(encoding="utf-8")
assert "set simulationPort for every receive port when using --simulate" in application

print("simulation configuration regression passed")
