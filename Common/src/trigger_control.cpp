#include "functions.h"
#include "log_output.h"
#include <algorithm>
#include <arpa/inet.h>
#include <poll.h>
#include <sstream>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

static bool active(const EventInfo &e) {
    return e.state == "PREPARING" || e.state == "PREPARED" || e.state == "QUEUED" ||
           e.state == "SENDING" || e.state == "SEND_FAILED" || e.state == "STORED";
}
static std::string eventJson(const EventInfo &e) {
    return "\"event_id\":" + quoteJson(e.id) + ",\"status\":" + quoteJson(e.state) +
           ",\"start_period\":" + std::to_string(e.start) +
           ",\"period_count\":" + std::to_string(e.count) +
           ",\"missing_packets\":" + std::to_string(e.missing) +
           ",\"sent_bytes\":" + std::to_string(e.sentBytes) + ",\"error\":" + quoteJson(e.error);
}
static void rememberEvent(PARAMETERS &para, const EventInfo &event) {
    if (!para.history.count(event.id))
        para.historyOrder.push_back(event.id);
    para.history[event.id] = event;
    while (para.historyOrder.size() > 64) {
        para.history.erase(para.historyOrder.front());
        para.historyOrder.pop_front();
    }
}
static void remember(PARAMETERS &para) {
    rememberEvent(para, para.event);
}
// Protect all receive ports before collating the event; clean up any started freeze operations on failure.
static void prepareEvent(PARAMETERS *para) {
    size_t begun = 0;
    size_t finished = 0;
    try {
        // Redirect every port first, so no port overwrites its event while
        // another port is being consolidated.
        for (auto &pool : para->pEthRcvPool) {
            pool->beginFreeze(para->event.start, para->event.count);
            ++begun;
        }
        for (auto &pool : para->pEthRcvPool) {
            pool->finishFreeze();
            ++finished;
        }
        uint64_t missing = 0;
        for (auto &pool : para->pEthRcvPool)
            missing += pool->missingPackets();
        std::string message;
        {
            std::lock_guard<std::mutex> lock(para->eventMutex);
            para->event.missing = missing;
            if (para->event.cancelRequested) {
                for (auto &pool : para->pEthRcvPool)
                    pool->release();
                para->event.state = "CANCELLED";
                remember(*para);
            } else {
                para->event.state = "PREPARED";
            }
            message = "Event " + para->event.id + " " + para->event.state;
        }
        LOG(message, INFO);
    } catch (const std::exception &error) {
        // Finish any partially started freezes before releasing their banks.
        try {
            for (size_t i = finished; i < begun; ++i)
                para->pEthRcvPool[i]->finishFreeze();
            for (size_t i = 0; i < begun; ++i)
                para->pEthRcvPool[i]->release();
        } catch (const std::exception &cleanup) {
            LOG(cleanup.what(), FATAL);
            para->failed = true;
            para->flags->threadStop();
        }
        std::string message;
        {
            std::lock_guard<std::mutex> lock(para->eventMutex);
            para->event.state = "REJECTED";
            para->event.error = error.what();
            remember(*para);
            message = "Event rejected: " + para->event.error;
        }
        LOG(message, WARN);
    }
}
// Validate control requests and advance node event state; allow RELEASE only after persistence is confirmed.
std::string ProcessControl(PARAMETERS &para, const std::string &request) {
    std::string requestId;
    try {
        auto msg = parseControl(request);
        requestId = getId(msg, "request_id");
        auto command = getId(msg, "command");
        if (getId(msg, "acquisition_id") != para.config->acquisitionId)
            throw std::runtime_error("ACQUISITION_MISMATCH");
        std::lock_guard<std::mutex> lock(para.eventMutex);
        auto reply = [&](const std::string &body) {
            return "{\"version\":1,\"request_id\":" + quoteJson(requestId) +
                   ",\"server_id\":" + std::to_string(para.config->serverId) + "," + body + "}";
        };
        if (command == "QUERY_STATUS") {
            uint64_t oldest = 0;
            uint64_t end = UINT64_MAX;
            bool ready = !active(para.event) && !para.failed;
            for (const auto &pool : para.pEthRcvPool) {
                auto s = pool->status();
                oldest = std::max(oldest, s.oldest);
                end = std::min(end, s.settledEnd);
                ready = ready && s.ready;
            }
            ready = ready && para.initializedPorts == para.pEthRcvPool.size();
            return reply("\"status\":" +
                         quoteJson(para.failed          ? "ERROR"
                                   : ready              ? "READY"
                                   : active(para.event) ? "BUSY"
                                                        : "WARMING") +
                         ",\"accept_trigger\":" + (ready ? "true" : "false") +
                         ",\"oldest_period\":" + std::to_string(oldest) + ",\"settled_end\":" +
                         std::to_string(end) + ",\"event_id\":" + quoteJson(para.event.id) +
                         ",\"event_state\":" + quoteJson(para.event.state) +
                         ",\"initialized_ports\":" + std::to_string(para.initializedPorts.load()) +
                         ",\"ignored_packets\":" + std::to_string(para.ignoredPackets.load()));
        }
        if (command == "QUERY_PORT") {
            uint64_t p = getNumber(msg, "port");
            if (p >= para.pEthRcvPool.size())
                throw std::runtime_error("INVALID_PORT");
            auto s = para.pEthRcvPool[p]->status();
            const auto &port = para.config->receivePorts[p];
            return reply("\"status\":\"OK\",\"port\":" + std::to_string(p) +
                         ",\"packet_id\":" + std::to_string(port.packetId) +
                         ",\"board_begin\":" + std::to_string(port.boardBegin) +
                         ",\"oldest_period\":" + std::to_string(s.oldest) + ",\"packet_id_end\":" +
                         std::to_string(port.packetId + port.packetsPerBoard - 1) +
                         ",\"board_count\":" + std::to_string(port.boardNum) +
                         ",\"packets_per_board\":" + std::to_string(port.packetsPerBoard) +
                         ",\"packet_slots\":" + std::to_string(port.packetSlots()) +
                         ",\"frequency_begin\":" + std::to_string(port.frequencyBegin) +
                         ",\"frequency_count\":" + std::to_string(port.frequencyCount) +
                         ",\"valid_frequency_count\":" + std::to_string(port.validFrequencyCount) +
                         ",\"settled_end\":" + std::to_string(s.settledEnd) +
                         ",\"latest_period\":" + std::to_string(s.latest) +
                         ",\"received_packets\":" + std::to_string(s.received) +
                         ",\"duplicate_packets\":" + std::to_string(s.duplicate) +
                         ",\"late_packets\":" + std::to_string(s.late) +
                         ",\"conflicting_packets\":" + std::to_string(s.conflicting));
        }
        auto id = getId(msg, "event_id");
        if (command == "PREPARE" || command == "CAPTURE") {
            if (command == "CAPTURE")
                throw std::runtime_error("USE_CONTROLLER_FOR_CAPTURE");
            uint64_t trace = getNumber(msg, "start.trace_period_id");
            uint64_t integration = getNumber(msg, "start.integration_period_id");
            uint64_t count = getNumber(msg, "period_count");
            uint64_t epochEnd = (1ULL << 20) * para.config->integrationPerTrace;
            if (trace >= (1ULL << 20) || integration >= para.config->integrationPerTrace ||
                !count || count > para.config->maxEventPeriods())
                throw std::runtime_error("INVALID_RANGE");
            uint64_t start = trace * para.config->integrationPerTrace + integration;
            if (count > epochEnd - start)
                throw std::runtime_error("COUNTER_WRAP_REQUIRES_NEW_ACQUISITION");
            const EventInfo *existing = nullptr;
            if (para.event.id == id)
                existing = &para.event;
            else if (para.history.count(id))
                existing = &para.history.at(id);
            if (existing) {
                if (existing->start != start || existing->count != count)
                    throw std::runtime_error("EVENT_ID_CONFLICT");
                return reply(eventJson(*existing));
            }
            if (para.failed || active(para.event))
                throw std::runtime_error("BUSY");
            if (para.initializedPorts != para.pEthRcvPool.size())
                throw std::runtime_error("NOT_READY");
            for (const auto &pool : para.pEthRcvPool) {
                auto s = pool->status();
                if (!s.ready)
                    throw std::runtime_error("WARMING");
                if (start < s.oldest)
                    throw std::runtime_error("EXPIRED");
                if (start + count > s.settledEnd)
                    throw std::runtime_error("NOT_READY");
            }
            if (para.eventThread.joinable())
                para.eventThread.join();
            para.event = EventInfo{};
            para.event.id = id;
            para.event.start = start;
            para.event.count = count;
            para.event.state = "PREPARING";
            try {
                para.eventThread = std::thread(prepareEvent, &para);
            } catch (...) {
                para.event.state = "REJECTED";
                para.event.error = "prepare thread creation failed";
                remember(para);
                throw;
            }
            return reply(eventJson(para.event));
        }
        if (id != para.event.id) {
            auto old = para.history.find(id);
            if (old != para.history.end())
                return reply(eventJson(old->second));
            if (command == "CANCEL") {
                // Tombstone prevents a delayed PREPARE from resurrecting a cancelled event.
                EventInfo cancelled;
                cancelled.id = id;
                cancelled.state = "CANCELLED";
                rememberEvent(para, cancelled);
                return reply(eventJson(cancelled));
            }
            throw std::runtime_error("EVENT_NOT_FOUND");
        }
        auto &e = para.event;
        if (command == "QUERY_EVENT")
            return reply(eventJson(e));
        if (command == "COMMIT") {
            if (e.state == "PREPARED") {
                e.committed = true;
                e.state = "QUEUED";
            } else if (!e.committed)
                throw std::runtime_error("NOT_PREPARED");
        } else if (command == "SEND") {
            if (e.state == "QUEUED" || e.state == "SEND_FAILED") {
                if (para.eventThread.joinable())
                    para.eventThread.join();
                e.state = "SENDING";
                e.sentBytes = 0;
                e.error.clear();
                try {
                    para.eventThread = std::thread(InitSendStoreThread, &para);
                } catch (...) {
                    e.state = "SEND_FAILED";
                    e.error = "send thread creation failed";
                    throw;
                }
            } else if (e.state != "SENDING" && e.state != "STORED")
                throw std::runtime_error("NOT_COMMITTED");
        } else if (command == "RELEASE") {
            if (e.state == "STORED") {
                for (auto &pool : para.pEthRcvPool)
                    pool->release();
                e.state = "RELEASED";
                remember(para);
            } else if (e.state != "RELEASED")
                throw std::runtime_error("NOT_DURABLY_STORED");
        } else if (command == "CANCEL") {
            if (e.state == "PREPARING")
                e.cancelRequested = true;
            else if (e.state == "PREPARED" || e.state == "QUEUED" || e.state == "SEND_FAILED") {
                for (auto &pool : para.pEthRcvPool)
                    pool->release();
                e.state = "CANCELLED";
                remember(para);
            } else if (e.state != "CANCELLED" && e.state != "REJECTED")
                throw std::runtime_error("CANNOT_CANCEL_CURRENT_STATE");
        } else
            throw std::runtime_error("UNKNOWN_COMMAND");
        return reply(eventJson(e));
    } catch (const std::exception &e) {
        return "{\"version\":1,\"request_id\":" + quoteJson(requestId) +
               ",\"server_id\":" + std::to_string(para.config->serverId) +
               ",\"status\":\"ERROR\",\"error\":" + quoteJson(e.what()) + "}";
    }
}
void InitControlThread(PARAMETERS *para) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        throw std::runtime_error("control socket failed");
    struct Close {
        int fd;
        ~Close() {
            close(fd);
        }
    } closeFd{fd};
    sockaddr_in addr{}, allowed{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(para->config->controlPort);
    inet_pton(AF_INET, para->config->controlBindIp.c_str(), &addr.sin_addr);
    inet_pton(AF_INET, para->config->controlPeerIp.c_str(), &allowed.sin_addr);
    if (bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)))
        throw std::runtime_error("control bind failed");
    LOG("UDP control ready, acquisition=" + para->config->acquisitionId, START);
    while (para->flags->ifControl) {
        pollfd p{fd, POLLIN, 0};
        if (poll(&p, 1, 100) <= 0)
            continue;
        char buffer[MAX_CONTROL_BYTES + 1];
        sockaddr_in peer{};
        socklen_t size = sizeof(peer);
        auto n = recvfrom(fd, buffer, sizeof(buffer), MSG_TRUNC,
                          reinterpret_cast<sockaddr *>(&peer), &size);
        if (n < 0 || n > ssize_t(MAX_CONTROL_BYTES) ||
            peer.sin_addr.s_addr != allowed.sin_addr.s_addr)
            continue;
        auto response = ProcessControl(*para, std::string(buffer, size_t(n)));
        if (response.size() <= MAX_CONTROL_BYTES)
            sendto(fd, response.data(), response.size(), 0, reinterpret_cast<sockaddr *>(&peer),
                   size);
    }
}
