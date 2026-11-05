#include "functions.h"
#include "zmq_store.h"
#include "log_output.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <stdexcept>
#include <thread>

static void sendJson(StoreConnection &socket, const std::string &json) {
    std::vector<unsigned char> message;
    put32(message, json.size());
    message.insert(message.end(), json.begin(), json.end());
    // A retry after a lost final reply may be completed by the receiver here,
    // without retransmitting data that is already durably stored.
    socket.sendMessage(message, false, true);
}

static void set32(std::vector<unsigned char> &buffer, size_t offset, uint32_t value) {
    for (unsigned i = 0; i < 4; ++i)
        buffer[offset + i] = static_cast<unsigned char>(value >> (24 - 8 * i));
}

// Assemble event data blocks by receive port and period, send each block, and validate the final persistence acknowledgement.
static void sendLane(CacheRuntime &para, unsigned lane, const EventInfo &event) {
    StoreConnection socket(para, lane);
    const auto &c = *para.config;
    // Remote input has two FFT halves per board. Require explicit metadata at the receiver;
    // keep the existing two profiles' preamble and binary record layout unchanged.
    const std::string profileFields =
        c.mode == StoreMode::F11_13
            ? ",\"profile\":\"remote96\",\"packets_per_board\":2,\"physical_board_count\":12,"
              "\"frequency_bins_per_port\":86,\"total_valid_frequencies\":1024,\"fft_per_packet\":"
              "12"
            : "";
    std::string portList = "[";
    for (unsigned p = lane; p < c.receivePorts.size(); p += 2) {
        if (portList.size() > 1)
            portList += ',';
        portList += std::to_string(p);
    }
    portList += ']';
    sendJson(socket,
             "{\"version\":1,\"format\":\"TLSTORE1\",\"event_id\":" + quoteJson(event.id) +
                 ",\"acquisition_id\":" + quoteJson(c.acquisitionId) + ",\"server_id\":" +
                 std::to_string(c.serverId) + ",\"lane\":" + std::to_string(lane) +
                 ",\"channel_count\":" + std::to_string(event.channelCount) +
                 ",\"port_count\":" + std::to_string(c.receivePorts.size()) + ",\"start_period\":" +
                 std::to_string(event.start) + ",\"period_count\":" + std::to_string(event.count) +
                 ",\"ports\":" + portList +
                 ",\"integration_per_trace\":" + std::to_string(c.integrationPerTrace) +
                 ",\"period_microseconds\":" + std::to_string(c.periodMicroseconds) +
                 ",\"packet_bytes\":" + std::to_string(c.inputDataBytes + INPUT_HEADER_BYTES) +
                 profileFields + "}");
    auto validateStored = [&] {
        auto ack = parseControl(socket.resultJson());
        if (getId(ack, "status") != "STORED" || getId(ack, "event_id") != event.id ||
            getId(ack, "acquisition_id") != c.acquisitionId || getNumber(ack, "lane") != lane ||
            getNumber(ack, "server_id") != c.serverId)
            throw std::runtime_error("invalid durable storage acknowledgment");
    };
    if (socket.hasResult()) {
        validateStored();
        return;
    }
    // Reuse one block across ports and chunks. The first 40 bytes are the wire header.
    std::vector<unsigned char> message;
    for (unsigned p = lane; p < c.receivePorts.size(); p += 2) {
        const auto &port = c.receivePorts[p];
        for (uint64_t start = event.start; start < event.start + event.count;) {
            uint64_t count = std::min<uint64_t>(c.chunkPeriods, event.start + event.count - start);
            message.clear();
            message.reserve(40 + count * (24ULL + port.packetSlots() *
                                                      (c.inputDataBytes + INPUT_HEADER_BYTES)));
            put32(message, 1);
            put32(message, p);
            put32(message, port.packetId);
            put32(message, port.boardBegin);
            put64(message, start);
            put64(message, count);
            put32(message, 0); // Body length, filled after the event frames are appended.
            put32(message, 0); // Reserved; must be zero.
            for (uint64_t i = 0; i < count; ++i)
                para.receiveBuffers[p]->appendEventFrame(start + i, message);
            const size_t bodyBytes = message.size() - 40;
            set32(message, 32, static_cast<uint32_t>(bodyBytes));
            socket.sendMessage(message);
            {
                std::lock_guard<std::mutex> lock(para.eventMutex);
                para.event.sentBytes += bodyBytes;
            }
            start += count;
        }
    }
    // A successful ZeroMQ send or per-block reply is not an event completion.
    // Keep the event frozen until the receiver returns the final STORED result.
    std::vector<unsigned char> end;
    put32(end, 2);
    end.resize(40, 0);
    socket.sendMessage(end, true);
    validateStored();
}
// Send frozen data from the event thread; retain the buffer and record SEND_FAILED on failure.
void InitSendStoreThread(CacheRuntime *para) {
    EventInfo event;
    {
        std::lock_guard<std::mutex> lock(para->eventMutex);
        event = para->event;
    }
    const auto started = std::chrono::steady_clock::now();
    std::array<std::string, 2> laneErrors;
    std::array<std::thread, 2> workers;
    try {
        for (unsigned lane = 0; lane < 2; ++lane) {
            workers[lane] = std::thread([&, lane] {
                try {
                    sendLane(*para, lane, event);
                } catch (const std::exception &caught) {
                    laneErrors[lane] = caught.what();
                }
            });
        }
    } catch (const std::exception &caught) {
        laneErrors[0] = caught.what();
    }
    for (auto &worker : workers)
        if (worker.joinable())
            worker.join();
    std::string error;
    for (unsigned lane = 0; lane < 2; ++lane)
        if (!laneErrors[lane].empty())
            error += (error.empty() ? "" : "; ") + std::string("lane ") +
                     std::to_string(lane) + ": " + laneErrors[lane];
    std::string logMessage;
    {
        std::lock_guard<std::mutex> lock(para->eventMutex);
        para->event.sendMilliseconds = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - started)
                .count());
        if (para->event.sendMilliseconds)
            para->event.sendRateBitsPerSecond =
                para->event.sentBytes * 8000ULL / para->event.sendMilliseconds;
        if (error.empty()) {
            para->event.state = EventState::Stored;
            para->event.error.clear();
        } else {
            para->event.state = EventState::SendFailed;
            para->event.error = error.substr(0, 300);
        }
        logMessage = "Event " + event.id + " " + eventStateName(para->event.state) +
                     " send_ms=" + std::to_string(para->event.sendMilliseconds) +
                     " rate_bps=" + std::to_string(para->event.sendRateBitsPerSecond) + " " +
                     para->event.error;
    }
    LOG(logMessage, INFO);
}
