#!/usr/bin/env python3
"""Keep field comments inline in user-facing YAML configuration files."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
files = sorted((ROOT / "F0-2").glob("*.yaml"))
files += sorted((ROOT / "F3-10").glob("*.yaml"))
files += sorted((ROOT / "F11-13").glob("*.yaml"))
files += [ROOT / "Storage/storage.yaml"]
files += [
    ROOT / "deploy/ansible/templates/recv.yaml.j2",
    ROOT / "deploy/ansible/templates/server.yaml.j2",
    ROOT / "deploy/ansible/templates/storage.yaml.j2",
]

standalone_field_comment = re.compile(r"^ {2,}# ", re.MULTILINE)
inline_comment = re.compile(r"^\s*[^#\s][^\n]*  # ", re.MULTILINE)
for path in files:
    text = path.read_text(encoding="utf-8")
    assert not standalone_field_comment.search(text), path
    assert inline_comment.search(text), path

for path in files:
    text = path.read_text(encoding="utf-8")
    assert "recvCfg:" not in text, path
    assert "serverCfg:" not in text, path
for path in [ROOT / "Storage/storage.yaml", ROOT / "deploy/ansible/templates/storage.yaml.j2"]:
    text = path.read_text(encoding="utf-8")
    assert "storage:" not in text, path
    assert "backend:" not in text, path
    for key in ("directory:", "ioThreads:", "expectedServerIds:", "receivers:", "backendAddress:",
                "backendPort:", "backendBindAddress:"):
        assert re.search(rf"^{key}", text, re.MULTILINE), (path, key)

print("YAML comment style regression passed")
