#include "functions.h"
#include "log_output.h"
#include "zmq_store.h"
#include <cassert>
#include <cstring>
#include <stdexcept>
#include <vector>
#include <zlib.h>

// The sender is real; the network socket is replaced with a capture sink.
static std::vector<std::vector<unsigned char>> messages;
static bool fail_final_ack = false;
static bool fail_data_ack = false;
static bool already_stored = false;
StoreConnection::StoreConnection(CacheRuntime &p, unsigned, StorePacer &limit) : para(p), pacer(limit) {}
StoreConnection::~StoreConnection() = default;
void StoreConnection::sendMessage(const std::vector<unsigned char> &message, bool final, bool allowStored) {
    messages.push_back(message);
    if (allowStored && already_stored) result.assign(1, 1);
    if (!final && fail_data_ack && messages.size() == 2)
        throw std::runtime_error("ZeroMQ data block acknowledgment timed out");
    if (final && fail_final_ack)
        throw std::runtime_error("ZeroMQ connection closed before durable acknowledgment");
}
std::string StoreConnection::resultJson() const {
    return "{\"version\":1,\"status\":\"STORED\",\"event_id\":\"event1\","
           "\"acquisition_id\":\"acq1\",\"server_id\":0,\"lane\":0}";
}
YamlConfig::YamlConfig(const std::string &, const std::string &, StoreMode profile) : mode(profile) {}
void logOut(const std::string &, LogLevel) {}

static uint64_t readNumber(const std::vector<unsigned char> &data, size_t offset, size_t count) {
    uint64_t value = 0;
    for (size_t i = 0; i < count; ++i)
        value = (value << 8) | data[offset + i];
    return value;
}

int main() {
    assert(crc32(0, reinterpret_cast<const unsigned char *>("123456789"), 9) == 0xcbf43926UL);
    YamlConfig config("", "", StoreMode::F0_2);
    config.serverId = 0;
    config.acquisitionId = "acq1";
    config.inputDataBytes = 16;
    config.chunkPeriods = 3;
    ReceivePortConfig port;
    port.packetId = 24;
    port.boardNum = 2;
    config.receivePorts.push_back(port);

    CacheRuntime params;
    params.config = &config;
    params.event.id = "event1";
    params.event.start = 8;
    params.event.count = 4;
    params.event.state = EventState::Sending;
    params.receiveBuffers.emplace_back(
        new ReceiveRingBuffer(0, 10, 20, 5, 2, 32, 2, 100000, 80, false));
    auto &pool = *params.receiveBuffers[0];
    for (unsigned period = 0; period < 20; ++period) {
        for (unsigned board = 0; board < 2; ++board) {
            unsigned char packet[32];
            std::memset(packet, static_cast<int>(period + board), sizeof(packet));
            if (period != 9 || board != 1)
                assert(pool.receive(period, board, 0, packet));
        }
    }
    pool.beginFreeze(8, 4);
    pool.finishFreeze();
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::Stored);
    assert(messages.size() == 4); // Metadata, two data chunks, END.
    size_t expectedBytes = 0;
    for (size_t block = 0; block < 2; ++block) {
        const uint64_t start = block == 0 ? 8 : 11;
        const uint64_t count = block == 0 ? 3 : 1;
        std::vector<unsigned char> body;
        for (uint64_t i = 0; i < count; ++i)
            pool.appendEventFrame(start + i, body);
        const auto &wire = messages[block + 1];
        assert(wire.size() == 40 + body.size());
        assert(readNumber(wire, 0, 4) == 1 && readNumber(wire, 4, 4) == 0);
        assert(readNumber(wire, 8, 4) == 24 && readNumber(wire, 12, 4) == 0);
        assert(readNumber(wire, 16, 8) == start && readNumber(wire, 24, 8) == count);
        assert(readNumber(wire, 32, 4) == body.size());
        assert(readNumber(wire, 36, 4) == crc32(0, body.data(), body.size()));
        assert(std::memcmp(wire.data() + 40, body.data(), body.size()) == 0);
        expectedBytes += body.size();
    }
    assert(params.event.sentBytes == expectedBytes);
    assert(messages.back().size() == 40 && readNumber(messages.back(), 0, 4) == 2);

    // Lost per-block credit must stop later messages and keep the data frozen.
    messages.clear();
    fail_data_ack = true;
    params.event.state = EventState::Sending;
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::SendFailed);
    assert(params.event.error.find("data block acknowledgment") != std::string::npos);
    assert(messages.size() == 2);
    std::vector<unsigned char> stillFrozen;
    pool.appendEventFrame(8, stillFrozen);
    assert(!stillFrozen.empty());

    // Sending all bytes is insufficient: a lost final acknowledgment must keep
    // the event failed and its frozen data available for a retry. If the receiver
    // did persist it, the retry is completed at the metadata acknowledgment.
    fail_data_ack = false;
    fail_final_ack = true;
    params.event.state = EventState::Sending;
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::SendFailed);
    assert(params.event.error.find("durable acknowledgment") != std::string::npos);
    stillFrozen.clear();
    pool.appendEventFrame(8, stillFrozen);
    assert(!stillFrozen.empty());

    // A retry whose data is already durable ends after the metadata exchange.
    messages.clear();
    fail_final_ack = false;
    already_stored = true;
    params.event.state = EventState::Sending;
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::Stored);
    assert(messages.size() == 1);
    pool.release();
}
