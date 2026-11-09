#!/usr/bin/env python3
"""Host regression; uses real pool/control/sender code, no NIC or GPU required."""
from pathlib import Path
import subprocess
import tempfile
import os
import shlex
import sys

root = Path(__file__).resolve().parents[1]
cxx = shlex.split(os.environ.get('CXX', 'g++'))
with tempfile.TemporaryDirectory(prefix='tianlai-store-tests-') as tmp:
    binary = str(Path(tmp) / 'receive_regression')
    subprocess.run(cxx + ['-std=c++14', '-O1', '-g', '-pthread',
                    '-DBOOST_BIND_GLOBAL_PLACEHOLDERS', '-fsanitize=address,undefined',
                    '-fno-omit-frame-pointer', '-I' + str(root / 'Common/include'),
                    str(root / 'tests/receive_regression.cpp'),
                    str(root / 'Common/src/receive_pool.cpp'), str(root / 'Common/src/protocol.cpp'),
                    '-o', binary], check=True)
    subprocess.run([binary], check=True, timeout=60)
    block_test = str(Path(tmp) / 'send_block_regression')
    subprocess.run(cxx + ['-std=c++14', '-O2', '-pthread', '-DBOOST_BIND_GLOBAL_PLACEHOLDERS',
                    '-I' + str(root / 'Common/include'), str(root / 'tests/send_block_regression.cpp'),
                    str(root / 'Common/src/send_store.cpp'), str(root / 'Common/src/receive_pool.cpp'),
                    str(root / 'Common/src/protocol.cpp'), '-o', block_test], check=True)
    subprocess.run([block_test], check=True, timeout=20)
    storage_test = str(Path(tmp) / 'storage_regression')
    subprocess.run(cxx + ['-std=c++17', '-O2', '-pthread',
                    str(root / 'tests/storage_receiver_regression.cpp'),
                    '-lzmq', '-o', storage_test], check=True)
    subprocess.run([storage_test, str(Path(tmp) / 'storage-data')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/storage_integration.py')], check=True, timeout=180)
subprocess.run([sys.executable, str(root / 'tests/integration.py')], check=True, timeout=180)
subprocess.run([sys.executable, str(root / 'tests/controller_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/notification_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/controller_safety_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/tcp_control_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/network_inventory_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/jumbo_mtu_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/acquisition_deployment_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/simulation_config_regression.py')], check=True, timeout=30)
subprocess.run([sys.executable, str(root / 'tests/yaml_comment_style_regression.py')], check=True, timeout=30)
