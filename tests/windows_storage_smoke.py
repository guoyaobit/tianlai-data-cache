#!/usr/bin/env python3
"""Exercise C++ storage logic on Windows with test-only POSIX filesystem shims.

Requires a local include directory containing zmq.h and nlohmann/json.hpp.
Does not test Linux directory fsync, network transport, or production throughput.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--includes', help='optional directory containing libzmq import library')
    parser.add_argument('--json-header', help='path to the nlohmann single-header json.hpp')
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--integration', action='store_true',
                        help='link temporary ZeroMQ DLL and run real loopback with pyzmq')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tianlai-cxx-storage-') as directory:
        tmp = Path(directory).resolve()
        if tmp.parent != Path(tempfile.gettempdir()).resolve() or not tmp.name.startswith('tianlai-cxx-storage-'):
            raise RuntimeError('unsafe temporary directory')
        (tmp / 'arpa').mkdir()
        (tmp / 'nlohmann').mkdir()
        if args.json_header:
            shutil.copyfile(args.json_header, tmp / 'nlohmann/json.hpp')
        elif args.includes and (Path(args.includes) / 'nlohmann/json.hpp').is_file():
            shutil.copyfile(Path(args.includes) / 'nlohmann/json.hpp', tmp / 'nlohmann/json.hpp')
        else:
            parser.error('--json-header or --includes/nlohmann/json.hpp is required')
        (tmp / 'zmq.h').write_text(r'''#pragma once
#include <cstddef>
#include <cstdint>
extern "C" {
typedef struct { unsigned char data[64]; } zmq_msg_t;
typedef struct { void* socket; uintptr_t fd; short events; short revents; } zmq_pollitem_t;
void* zmq_ctx_new(void); int zmq_ctx_set(void*, int, int); int zmq_ctx_term(void*);
void* zmq_socket(void*, int); int zmq_close(void*); int zmq_bind(void*, const char*);
int zmq_setsockopt(void*, int, const void*, size_t); int zmq_send(void*, const void*, size_t, int);
int zmq_poll(zmq_pollitem_t*, int, long); int zmq_errno(void); const char* zmq_strerror(int);
int zmq_msg_init(zmq_msg_t*); int zmq_msg_close(zmq_msg_t*); int zmq_msg_recv(zmq_msg_t*, void*, int);
void* zmq_msg_data(zmq_msg_t*); size_t zmq_msg_size(const zmq_msg_t*); int zmq_msg_more(const zmq_msg_t*);
const char* zmq_msg_gets(const zmq_msg_t*, const char*);
}
#define ZMQ_ROUTER 6
#define ZMQ_LINGER 17
#define ZMQ_ROUTER_MANDATORY 33
#define ZMQ_SNDHWM 23
#define ZMQ_RCVHWM 24
#define ZMQ_MAXMSGSIZE 22
#define ZMQ_SNDMORE 2
#define ZMQ_DONTWAIT 1
#define ZMQ_POLLIN 1
#define ZMQ_IO_THREADS 1
''')
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
inline int fdatasync(int) { return 0; }
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
                        '-fdata-sections', '-I' + str(tmp),
                        str(ROOT / 'tests/storage_receiver_regression.cpp'),
                        '-Wl,--gc-sections', '-o', str(binary)], check=True, timeout=120)
        subprocess.run([str(binary), str(tmp / 'data')], check=True, timeout=60)
        subprocess.run([args.cxx, '-std=c++17', '-O2', '-pthread', '-I' + str(tmp),
                        '-c', str(ROOT / 'tests/storage_test_server.cpp'),
                        '-o', str(tmp / 'storage_test_server.o')], check=True, timeout=120)
        if args.integration:
            service = tmp / 'storage_server.exe'
            if not args.includes:
                parser.error('--includes is required with --integration')
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
