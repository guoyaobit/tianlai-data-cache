"""Durable JSON journal helpers shared by the trigger controller."""
import json
import os
from pathlib import Path
import re


ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


def sync_directory(path):
    """Persist directory metadata after an atomic file replacement."""
    if os.name == 'nt':
        return  # Windows has no portable directory fsync; production runs on Linux.
    fd = os.open(Path(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    """Write a JSON object through a synced temporary file and atomic rename."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, separators=(',', ':'))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)
