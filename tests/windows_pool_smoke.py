#!/usr/bin/env python3
"""Run the real pool regression on Windows with a test-only mmap shim.

This exercises pool logic, not Linux mmap, ixcap, protocol parsing or sanitizers.
The Boost forward declaration and serialization stubs are used only in this build.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx', default='g++')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pool-smoke-', dir=ROOT) as directory:
        tmp = Path(directory).resolve()
        if tmp.parent != ROOT or not tmp.name.startswith('pool-smoke-'):
            raise RuntimeError('unsafe temporary directory')
        (tmp / 'sys').mkdir()
        (tmp / 'boost/property_tree').mkdir(parents=True)
        (tmp / 'boost/property_tree/ptree.hpp').write_text(
            'namespace boost { namespace property_tree { class ptree {}; } }\n')
        (tmp / 'sys/mman.h').write_text('''#pragma once
#include <windows.h>
#include <cstddef>
#define PROT_READ 1
#define PROT_WRITE 2
#define MAP_PRIVATE 1
#define MAP_ANONYMOUS 2
#define MAP_FAILED ((void*)-1)
#define MADV_HUGEPAGE 1
#define MADV_DONTDUMP 2
inline void* mmap(void*, size_t n, int, int, int, long) {
    void* p = VirtualAlloc(nullptr, n, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    return p ? p : MAP_FAILED;
}
inline int madvise(void*, size_t, int) { return 0; }
inline int munmap(void* p, size_t) { return VirtualFree(p, 0, MEM_RELEASE) ? 0 : -1; }
''')
        source = (ROOT / 'tests/receive_regression.cpp').read_text(encoding='utf-8')
        prefix, marker, _ = source.partition('    // Header bits and strict JSON fields.')
        if not marker:
            raise RuntimeError('pool regression boundary changed')
        (tmp / 'pool_test.cpp').write_text(
            prefix + '    std::cout << "portable receive pool regression passed\\n";\n}\n',
            encoding='utf-8')
        (tmp / 'protocol_stub.cpp').write_text('''#include "protocol.h"
void put32(std::vector<unsigned char>& v, uint32_t n) {
    for (int i=3; i>=0; --i) v.push_back((n>>(8*i))&255);
}
void put64(std::vector<unsigned char>& v, uint64_t n) {
    for (int i=7; i>=0; --i) v.push_back((n>>(8*i))&255);
}
std::string quoteJson(const std::string& value) { return "\\\"" + value + "\\\""; }
boost::property_tree::ptree parseControl(const std::string&) { return {}; }
std::string getId(const boost::property_tree::ptree&, const std::string& key) {
    return key == "status" ? "STORED" : key == "event_id" ? "event1" : "acq1";
}
uint64_t getNumber(const boost::property_tree::ptree&, const std::string&) { return 0; }
''')
        binary = tmp / 'pool_test.exe'
        subprocess.run([args.cxx, '-std=c++14', '-O2', '-pthread', '-I' + str(tmp),
                        '-I' + str(ROOT / 'Common/include'), str(tmp / 'pool_test.cpp'),
                        str(ROOT / 'Common/src/receive_pool.cpp'), str(tmp / 'protocol_stub.cpp'),
                        '-o', str(binary)], check=True, timeout=90)
        for _ in range(5):
            subprocess.run([str(binary)], check=True, timeout=60)
        (tmp / 'netinet').mkdir()
        (tmp / 'netinet/in.h').write_text('struct sockaddr_in {};\n')
        (tmp / 'zlib.h').write_text('''#pragma once
#include <cstdint>
inline unsigned long crc32(unsigned long value, const unsigned char* data, unsigned int count) {
    uint32_t crc = static_cast<uint32_t>(value) ^ 0xffffffffU;
    for (unsigned int i=0; i<count; ++i) {
        crc ^= data[i];
        for (int j=0; j<8; ++j) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
    }
    return crc ^ 0xffffffffU;
}
''')
        subprocess.run([args.cxx, '-std=c++14', '-pthread', '-fsyntax-only', '-I' + str(tmp),
                        '-I' + str(ROOT / 'Common/include'),
                        str(ROOT / 'Common/src/send_store.cpp')], check=True, timeout=90)
        subprocess.run([args.cxx, '-std=c++14', '-pthread', '-fsyntax-only', '-I' + str(tmp),
                        '-I' + str(ROOT / 'Common/include'),
                        str(ROOT / 'Common/src/event_state.cpp')], check=True, timeout=90)
        block_binary = tmp / 'send_block_test.exe'
        subprocess.run([args.cxx, '-std=c++14', '-O2', '-pthread', '-I' + str(tmp),
                        '-I' + str(ROOT / 'Common/include'), str(ROOT / 'tests/send_block_regression.cpp'),
                        str(ROOT / 'Common/src/send_store.cpp'), str(ROOT / 'Common/src/receive_pool.cpp'),
                        str(tmp / 'protocol_stub.cpp'), '-o', str(block_binary)], check=True, timeout=90)
        subprocess.run([str(block_binary)], check=True, timeout=60)
        print('portable send block regression passed')


if __name__ == '__main__':
    main()
