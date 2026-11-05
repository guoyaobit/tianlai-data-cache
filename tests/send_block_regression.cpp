#include "functions.h"
#include "log_output.h"
#include "zmq_store.h"
#include <cassert>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <vector>
#include <zlib.h>

// The sender is real; the network socket is replaced with a capture sink.
static std::vector<std::vector<unsigned char>> messages[2];
static std::mutex messagesMutex;
static bool fail_final_ack = false;
static bool fail_data_ack = false;
static bool already_stored = false;
StoreConnection::StoreConnection(CacheRuntime &p, unsigned lane, StorePacer &limit)
    : para(p), pacer(limit), laneIndex(lane) {}
StoreConnection::~StoreConnection() = default;
void StoreConnection::sendMessage(const std::vector<unsigned char> &message, bool final, bool allowStored) {
    std::lock_guard<std::mutex> lock(messagesMutex);
    messages[laneIndex].push_back(message);
    if (allowStored && already_stored) result.assign(1, 1);
    if (laneIndex == 0 && !final && fail_data_ack && messages[laneIndex].size() == 2)
        throw std::runtime_error("ZeroMQ data block acknowledgment timed out");
    if (final && fail_final_ack)
        throw std::runtime_error("ZeroMQ connection closed before durable acknowledgment");
}
std::string StoreConnection::resultJson() const {
    return "{\"version\":1,\"status\":\"STORED\",\"event_id\":\"event1\","
           "\"acquisition_id\":\"acq1\",\"server_id\":0,\"lane\":" +
           std::to_string(laneIndex) + "}";
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
    port.packetId = 25;
    config.receivePorts.push_back(port);

    CacheRuntime params;
    params.config = &config;
    params.event.id = "event1";
    params.event.start = 8;
    params.event.count = 4;
    params.event.state = EventState::Sending;
    params.receiveBuffers.emplace_back(
        new ReceiveRingBuffer(0, 10, 20, 5, 2, 32, 2, 100000, 80, false));
    params.receiveBuffers.emplace_back(
        new ReceiveRingBuffer(1, 10, 20, 5, 2, 32, 2, 100000, 80, false));
    auto &pool = *params.receiveBuffers[0];
    auto &pool1 = *params.receiveBuffers[1];
    for (unsigned period = 0; period < 20; ++period) {
        for (unsigned board = 0; board < 2; ++board) {
            unsigned char packet[32];
            std::memset(packet, static_cast<int>(period + board), sizeof(packet));
            if (period != 9 || board != 1)
                assert(pool.receive(period, board, 0, packet));
            assert(pool1.receive(period, board, 0, packet));
        }
    }
    pool.beginFreeze(8, 4);
    pool.finishFreeze();
    pool1.beginFreeze(8, 4);
    pool1.finishFreeze();
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::Stored);
    assert(messages[0].size() == 4 && messages[1].size() == 4);
    const std::string metadata(messages[0][0].begin() + 4, messages[0][0].end());
    assert(metadata.find("\"channel_count\":384") != std::string::npos);
    size_t expectedBytes = 0;
    for (size_t block = 0; block < 2; ++block) {
        const uint64_t start = block == 0 ? 8 : 11;
        const uint64_t count = block == 0 ? 3 : 1;
        std::vector<unsigned char> body;
        for (uint64_t i = 0; i < count; ++i)
            pool.appendEventFrame(start + i, body);
        const auto &wire = messages[0][block + 1];
        assert(wire.size() == 40 + body.size());
        assert(readNumber(wire, 0, 4) == 1 && readNumber(wire, 4, 4) == 0);
        assert(readNumber(wire, 8, 4) == 24 && readNumber(wire, 12, 4) == 0);
        assert(readNumber(wire, 16, 8) == start && readNumber(wire, 24, 8) == count);
        assert(readNumber(wire, 32, 4) == body.size());
        assert(readNumber(wire, 36, 4) == crc32(0, body.data(), body.size()));
        assert(std::memcmp(wire.data() + 40, body.data(), body.size()) == 0);
        expectedBytes += body.size();
    }
    assert(params.event.sentBytes == expectedBytes * 2);
    assert(messages[0].back().size() == 40 && readNumber(messages[0].back(), 0, 4) == 2);
    assert(messages[1].back().size() == 40 && readNumber(messages[1].back(), 0, 4) == 2);

    // Lost per-block credit must stop later messages and keep the data frozen.
    messages[0].clear(); messages[1].clear();
    fail_data_ack = true;
    params.event.state = EventState::Sending;
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::SendFailed);
    assert(params.event.error.find("data block acknowledgment") != std::string::npos);
    assert(messages[0].size() == 2 && messages[1].size() == 4);
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
    messages[0].clear(); messages[1].clear();
    fail_final_ack = false;
    already_stored = true;
    params.event.state = EventState::Sending;
    InitSendStoreThread(&params);
    assert(params.event.state == EventState::Stored);
    assert(messages[0].size() == 1 && messages[1].size() == 1);
    pool.release();
    pool1.release();
}
