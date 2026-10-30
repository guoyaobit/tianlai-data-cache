#!/usr/bin/env python3
"""Exercise C++ storage logic on Windows with test-only POSIX filesystem shims.

Requires a local include directory containing zmq.h and nlohmann/json.hpp.
Does not test Linux directory fsync, network transport, or production throughput.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--includes', required=True)
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--integration', action='store_true',
                        help='link temporary ZeroMQ DLL and run real loopback with pyzmq')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tianlai-cxx-storage-') as directory:
        tmp = Path(directory).resolve()
        if tmp.parent != Path(tempfile.gettempdir()).resolve() or not tmp.name.startswith('tianlai-cxx-storage-'):
            raise RuntimeError('unsafe temporary directory')
        (tmp / 'arpa').mkdir()
        (tmp / 'arpa/inet.h').write_text('#include <winsock2.h>\n#include <ws2tcpip.h>\n')
        (tmp / 'fcntl.h').write_text(r'''#pragma once
#include_next <fcntl.h>
#include <io.h>
#include <stdarg.h>
#include <windows.h>
#define O_DIRECTORY 0x100000
#define O_CLOEXEC 0
inline int storage_test_open(const char* path, int flags, ...) {
    if (flags & O_DIRECTORY) return _dup(1);
    return _open(path, flags | _O_BINARY, 0640);
}
inline int storage_test_rename(const char* from, const char* to) {
    return MoveFileExA(from, to, MOVEFILE_REPLACE_EXISTING) ? 0 : -1;
}
#define open storage_test_open
#define rename storage_test_rename
''')
        (tmp / 'unistd.h').write_text(r'''#pragma once
#include <io.h>
#include <sys/types.h>
inline int fsync(int) { return 0; }
inline int ftruncate(int fd, off_t n) { return _chsize_s(fd, n) ? -1 : 0; }
inline int pwrite(int fd, const void* data, size_t n, off_t offset) {
    if (_lseeki64(fd, offset, SEEK_SET) < 0) return -1;
    return _write(fd, data, static_cast<unsigned>(n));
}
''')
        (tmp / 'zlib.h').write_text(r'''#pragma once
#include <cstdint>
inline unsigned long crc32(unsigned long value, const unsigned char* data, unsigned int n) {
    uint32_t crc = static_cast<uint32_t>(value) ^ 0xffffffffU;
    while (n--) {
        crc ^= *data++;
        for (int i = 0; i < 8; ++i) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
    }
    return crc ^ 0xffffffffU;
}
''')
        binary = tmp / 'storage_test.exe'
        subprocess.run([args.cxx, '-std=c++17', '-O2', '-pthread', '-ffunction-sections',
                        '-fdata-sections', '-I' + str(tmp), '-I' + args.includes,
                        str(ROOT / 'tests/storage_receiver_regression.cpp'),
                        '-Wl,--gc-sections', '-o', str(binary)], check=True, timeout=120)
        subprocess.run([str(binary), str(tmp / 'data')], check=True, timeout=60)
        subprocess.run([args.cxx, '-std=c++17', '-O2', '-pthread', '-I' + str(tmp),
                        '-I' + args.includes, '-c', str(ROOT / 'tests/storage_test_server.cpp'),
                        '-o', str(tmp / 'storage_test_server.o')], check=True, timeout=120)
        if args.integration:
            service = tmp / 'storage_server.exe'
            deps = Path(args.includes).resolve()
            subprocess.run([args.cxx, '-std=c++17', '-O2', '-pthread', '-I' + str(tmp),
                            '-I' + str(deps), str(ROOT / 'tests/storage_test_server.cpp'),
                            str(deps / 'libzmq.dll.a'), '-lws2_32', '-o', str(service)],
                           check=True, timeout=120)
            environment = os.environ.copy()
            environment['PATH'] = str(deps) + os.pathsep + str(Path(args.cxx).parent) + os.pathsep + environment['PATH']
            subprocess.run([sys.executable, str(ROOT / 'tests/storage_integration.py'),
                            '--binary', str(service), '--test-cli'], env=environment,
                           check=True, timeout=120)


if __name__ == '__main__':
    main()
