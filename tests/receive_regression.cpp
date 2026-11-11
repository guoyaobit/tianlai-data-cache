#include "receive_pool.h"
#include "protocol.h"
#include "log_output.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <functional>
#include <iostream>
#include <mutex>
#include <thread>

static std::mutex logMutex;
static std::vector<std::string> warningLogs;
void logOut(const std::string &message, LogLevel level) {
    if (level == WARN) {
        std::lock_guard<std::mutex> lock(logMutex);
        warningLogs.push_back(message);
    }
}

static void rejects(const std::function<void()> &f) {
    bool caught = false;
    try { f(); } catch (const std::exception &) { caught = true; }
    assert(caught);
}
static std::vector<unsigned char> packet(uint64_t p, unsigned b) {
    std::vector<unsigned char> v(32, 0);
    v[0] = 0xf0 | ((p / 50000) >> 16);
    v[1] = (p / 50000) >> 8; v[2] = p / 50000;
    uint64_t integration = p % 50000;
    v[3] = integration >> 16; v[4] = integration >> 8; v[5] = integration;
    v[6] = b; v[7] = 24;
    for (unsigned i = 8; i < v.size(); ++i) v[i] = p + b + i;
    return v;
}
static void feed(ReceiveRingBuffer &pool, uint64_t p, unsigned mask = 3) {
    for (unsigned b = 0; b < 2; ++b) {
        auto v = packet(p, b);
        if (mask & (1 << b)) assert(pool.receive(p, b, 0, v.data()));
    }
}
static uint64_t number(const std::vector<unsigned char> &v, size_t at, size_t n) {
    uint64_t value = 0;
    for (size_t i = 0; i < n; ++i) value = (value << 8) | v[at + i];
    return value;
}
static void check(ReceiveRingBuffer &pool, uint64_t p, unsigned mask = 3) {
    std::vector<unsigned char> out;
    pool.appendEventFrame(p, out);
    assert(out.size() == 24 + 64 && number(out, 0, 8) == p && number(out, 8, 8) == mask);
    for (unsigned b = 0; b < 2; ++b) {
        auto expected = (mask & (1 << b)) ? packet(p, b) : std::vector<unsigned char>(32, 0);
        assert(!std::memcmp(out.data() + 24 + b * 32, expected.data(), 32));
    }
}
int main() {
    // Report a missing packet only after its period leaves the reorder window.
    ReceiveRingBuffer lossAlert(7, 10, 20, 5, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 7; ++p)
        feed(lossAlert, p, p == 3 ? 1 : 3);
    {
        std::lock_guard<std::mutex> lock(logMutex);
        assert(std::find(warningLogs.begin(), warningLogs.end(),
                         "Receive port 7 packet loss: missing_packets=1 affected_periods=1 "
                         "period_range=3..3") != warningLogs.end());
    }
    // An out-of-order period completed inside the reorder window is not loss.
    ReceiveRingBuffer reordered(8, 10, 20, 5, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 3; ++p)
        feed(reordered, p);
    feed(reordered, 4);
    feed(reordered, 3);
    feed(reordered, 5);
    {
        std::lock_guard<std::mutex> lock(logMutex);
        assert(std::none_of(warningLogs.begin(), warningLogs.end(), [](const std::string &line) {
            return line.find("Receive port 8 packet loss") != std::string::npos;
        }));
    }
    for (unsigned shift : {0U, 3U, 9U}) {
        ReceiveRingBuffer pool(0, 10, 20, 5, 2, 1, 32, 2, 100000, 25, false);
        for (unsigned p = shift; p < shift + 20; ++p) feed(pool, p);
        assert(pool.status().ready);
        rejects([&] { pool.beginFreeze(shift + 19, 1); });
        pool.beginFreeze(shift + 8, 4);
        for (unsigned p = shift + 20; p < shift + 26; ++p) feed(pool, p);
        auto transition = pool.status();
        assert(transition.transitionUsed == 8 && transition.transitionPeak == 8);
        assert(transition.transitionWarning && transition.transitionCapacity == 20);
        pool.finishFreeze();
        assert(pool.status().freezeCount == 1);
        for (unsigned p = shift + 8; p < shift + 12; ++p) check(pool, p);
        for (unsigned p = shift + 26; p < shift + 81; ++p) feed(pool, p);
        for (unsigned p = shift + 8; p < shift + 12; ++p) check(pool, p);
        rejects([&] { pool.beginFreeze(shift + 79, 1); });
        pool.release();
        assert(!pool.status().ready);
        for (unsigned p = shift + 81; p < shift + 91; ++p) feed(pool, p);
        assert(pool.status().ready);
        pool.beginFreeze(shift + 79, 4);
        pool.finishFreeze();
        for (unsigned p = shift + 79; p < shift + 83; ++p) check(pool, p);
        pool.release();
    }
    // Missing whole periods, missing boards, duplicates, out-of-order and conflicting flags.
    ReceiveRingBuffer pool(0, 10, 20, 5, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 20; ++p) {
        if (p != 9) feed(pool, p, p == 10 ? 1 : 3);
    }
    auto v = packet(19, 0);
    assert(!pool.receive(19, 0, 0, v.data()));
    assert(!pool.receive(19, 0, 1, v.data()));
    assert(!pool.receive(1, 0, 0, v.data()));
    assert(pool.status().duplicate == 1 && pool.status().conflicting == 1 && pool.status().late == 1);
    pool.beginFreeze(8, 4);
    rejects([&] { pool.receive(38, 0, 0, v.data()); });
    pool.finishFreeze();
    check(pool, 8); check(pool, 9, 0); check(pool, 10, 1); check(pool, 11);
    // Export complete, missing and partial frames into an existing output buffer.
    // This checks concatenation as used by the storage sender, not just single frames.
    std::vector<unsigned char> combined(7, 0xa5);
    pool.appendEventFrame(8, combined);
    pool.appendEventFrame(9, combined);
    pool.appendEventFrame(10, combined);
    assert(combined.size() == 7 + 3 * (24 + 64));
    for (size_t i = 0; i < 7; ++i) assert(combined[i] == 0xa5);
    assert(number(combined, 7, 8) == 8 && number(combined, 7 + 8, 8) == 3);
    assert(number(combined, 7 + 88, 8) == 9 && number(combined, 7 + 88 + 8, 8) == 0);
    assert(number(combined, 7 + 176, 8) == 10 && number(combined, 7 + 176 + 8, 8) == 1);
    assert(pool.missingPackets() == 3);
    pool.release();
    // A reused slot may contain bytes from an old cycle: missing packets still export zeros.
    ReceiveRingBuffer reused(0, 10, 20, 5, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 40; ++p) feed(reused, p, p == 20 ? 1 : 3);
    reused.beginFreeze(20, 1);
    reused.finishFreeze();
    check(reused, 20, 1);
    reused.release();
    // Reject a transition area that cannot hold the current unsettled tail.
    ReceiveRingBuffer undersized(0, 10, 1, 5, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 20; ++p) feed(undersized, p);
    rejects([&] { undersized.beginFreeze(8, 4); });
    assert(!undersized.isFrozen());
    // Exercise concurrent ingress during the transition and while exporting the frozen bank.
    ReceiveRingBuffer concurrent(0, 1000, 4000, 500, 2, 1, 32, 2, 100000, 80, false);
    for (unsigned p = 0; p < 2000; ++p) feed(concurrent, p);
    concurrent.beginFreeze(900, 300);
    std::thread producer([&] { for (unsigned p = 2000; p < 5000; ++p) feed(concurrent, p); });
    concurrent.finishFreeze(); producer.join();
    for (unsigned p = 900; p < 1200; ++p) check(concurrent, p);
    concurrent.release();
    // A remote receive port exposes one BoardBuffer view per physical board.
    // Each physical board contributes two adjacent FFT-half packet slots.
    ReceiveRingBuffer remote(0, 10, 20, 5, 4, 2, 32, 2, 100000, 80, false);
    assert(remote.packetSlotCount() == 4);
    assert(remote.packetsPerPhysicalBoard() == 2);
    assert(remote.physicalBoardCount() == 2);
    for (unsigned period = 0; period < 20; ++period) {
        for (unsigned packetSlot = 0; packetSlot < 4; ++packetSlot) {
            auto data = packet(period, packetSlot);
            assert(remote.receive(period, packetSlot, 0, data.data()));
        }
    }
    remote.beginFreeze(8, 1);
    remote.finishFreeze();
    std::vector<unsigned char> remoteFrame;
    remote.appendEventFrame(8, remoteFrame);
    assert(remoteFrame.size() == 24 + 4 * 32);
    assert(number(remoteFrame, 8, 8) == 0x0f);
    for (unsigned packetSlot = 0; packetSlot < 4; ++packetSlot) {
        auto expected = packet(8, packetSlot);
        assert(!std::memcmp(remoteFrame.data() + 24 + packetSlot * 32,
                            expected.data(), expected.size()));
    }
    // Header bits and strict JSON fields.
    auto p = packet(120ULL * 50000 + 45000, 35);
    auto h = decodeInput(p.data());
    assert(h.trace == 120 && h.integration == 45000 && h.board == 35 && h.packetId == 24);
    rejects([] { parseControl("{\"version\":1,\"version\":1}"); });
    rejects([] { parseUnsigned("18446744073709551616"); });
    rejects([] { parseUnsigned("-1"); });
    rejects([] { parseControl("[]"); });
    std::vector<unsigned char> eth(14 + 20 + 8 + p.size(), 0);
    eth[12] = 8; eth[14] = 0x45; eth[17] = 20 + 8 + p.size(); eth[23] = 17;
    eth[39] = 8 + p.size(); std::memcpy(eth.data() + 42, p.data(), p.size());
    const unsigned char *payload = nullptr; size_t length = 0;
    assert(inputPayload(eth.data(), eth.size(), payload, length) && length == p.size());
    eth[20] = 0x20; assert(!inputPayload(eth.data(), eth.size(), payload, length));
    eth[20] = 0;
    eth.insert(eth.begin() + 12, {0x81, 0, 0, 1});
    assert(inputPayload(eth.data(), eth.size(), payload, length) && length == p.size());
    std::cout << "receive/protocol regression passed\n";
}
