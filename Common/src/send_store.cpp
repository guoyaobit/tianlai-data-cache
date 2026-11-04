#include "functions.h"
#include "zmq_store.h"
#include "log_output.h"
#include <algorithm>
#include <chrono>
#include <stdexcept>
#include <zlib.h>

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
static void sendLane(CacheRuntime &para, unsigned lane, const EventInfo &event, StorePacer &pacer) {
    StoreConnection socket(para, lane, pacer);
    const auto &c = *para.config;
    // Remote input has two FFT halves per board. Require explicit metadata at the receiver;
    // keep the existing two profiles' preamble and binary record layout unchanged.
    const std::string profileFields =
        c.mode == StoreMode::F11_13
            ? ",\"profile\":\"remote96\",\"packets_per_board\":2,\"physical_board_count\":12,"
              "\"frequency_bins_per_port\":86,\"total_valid_frequencies\":1024,\"fft_per_packet\":"
              "12"
            : "";
    sendJson(socket,
             "{\"version\":1,\"format\":\"TLSTORE1\",\"event_id\":" + quoteJson(event.id) +
                 ",\"acquisition_id\":" + quoteJson(c.acquisitionId) + ",\"server_id\":" +
                 std::to_string(c.serverId) + ",\"lane\":" + std::to_string(lane) +
                 ",\"port_count\":" + std::to_string(c.receivePorts.size()) + ",\"start_period\":" +
                 std::to_string(event.start) + ",\"period_count\":" + std::to_string(event.count) +
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
    for (unsigned p = 0; p < c.receivePorts.size(); ++p) {
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
            put32(message, 0); // Body CRC32.
            for (uint64_t i = 0; i < count; ++i)
                para.receiveBuffers[p]->appendEventFrame(start + i, message);
            const size_t bodyBytes = message.size() - 40;
            const uint32_t checksum = crc32(0, message.data() + 40, bodyBytes);
            set32(message, 32, static_cast<uint32_t>(bodyBytes));
            set32(message, 36, checksum);
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
    StorePacer pacer(para->config->sendRateBitsPerSecond);
    const auto started = std::chrono::steady_clock::now();
    std::string error;
    try {
        sendLane(*para, 0, event, pacer);
    } catch (const std::exception &e) {
        error = e.what();
    }
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
