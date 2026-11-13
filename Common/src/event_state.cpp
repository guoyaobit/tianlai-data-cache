#include "functions.h"
#include "log_output.h"
#include <algorithm>
#include <chrono>
#include <sstream>
#include <stdexcept>

namespace {
constexpr uint64_t DATA_FLOW_TIMEOUT_MILLISECONDS = 2000;
}

static std::string eventJson(const EventInfo &e) {
    return "\"event_id\":" + quoteJson(e.id) + ",\"status\":" +
           quoteJson(eventStateName(e.state)) +
           ",\"channel_count\":" + std::to_string(e.channelCount) +
           ",\"server_mask\":" + std::to_string(e.serverMask) +
           ",\"start_period\":" + std::to_string(e.start) +
           ",\"period_count\":" + std::to_string(e.count) +
           ",\"missing_packets\":" + std::to_string(e.missing) +
           ",\"sent_bytes\":" + std::to_string(e.sentBytes) +
           ",\"freeze_ms\":" + std::to_string(e.freezeMilliseconds) +
           ",\"send_ms\":" + std::to_string(e.sendMilliseconds) +
           ",\"send_rate_bps\":" + std::to_string(e.sendRateBitsPerSecond) +
           ",\"transition_peak_periods\":" + std::to_string(e.transitionPeakPeriods) +
           ",\"transition_capacity_periods\":" +
           std::to_string(e.transitionCapacityPeriods) + ",\"error\":" + quoteJson(e.error);
}
static void rememberEvent(CacheRuntime &para, const EventInfo &event) {
    if (!para.history.count(event.id))
        para.historyOrder.push_back(event.id);
    para.history[event.id] = event;
    while (para.historyOrder.size() > 64) {
        para.history.erase(para.historyOrder.front());
        para.historyOrder.pop_front();
    }
}
static void remember(CacheRuntime &para) {
    rememberEvent(para, para.event);
}
// Protect all receive ports in place; clean up any started freeze operations on failure.
static void prepareEvent(CacheRuntime *para) {
    size_t begun = 0;
    size_t finished = 0;
    const auto started = std::chrono::steady_clock::now();
    try {
        // Protect every port first. This is constant-time and leaves receive
        // threads running while the event remains in its original slots.
        for (auto &pool : para->receiveBuffers) {
            pool->beginFreeze(para->event.start, para->event.count);
            ++begun;
        }
        for (auto &pool : para->receiveBuffers) {
            pool->finishFreeze();
            ++finished;
        }
        uint64_t missing = 0;
        uint64_t transitionPeak = 0;
        uint64_t transitionCapacity = 0;
        for (auto &pool : para->receiveBuffers) {
            missing += pool->missingPackets();
            const auto status = pool->status();
            transitionPeak = std::max(transitionPeak, status.transitionPeak);
            transitionCapacity = std::max(transitionCapacity, status.transitionCapacity);
        }
        std::string message, debugMessage;
        bool prepared = false;
        {
            std::lock_guard<std::mutex> lock(para->eventMutex);
            para->event.missing = missing;
            para->event.freezeMilliseconds = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - started)
                    .count());
            para->event.transitionPeakPeriods = transitionPeak;
            para->event.transitionCapacityPeriods = transitionCapacity;
            if (para->event.cancelRequested) {
                for (auto &pool : para->receiveBuffers)
                    pool->release();
                para->event.state = EventState::Cancelled;
                remember(*para);
            } else {
                para->event.state = EventState::Prepared;
                prepared = true;
            }
            message = "Event " + para->event.id + " " + eventStateName(para->event.state) +
                      " freeze_ms=" + std::to_string(para->event.freezeMilliseconds);
            debugMessage =
                "Event " + para->event.id +
                " details: start_period=" + std::to_string(para->event.start) +
                " period_count=" + std::to_string(para->event.count) +
                " missing_packets=" + std::to_string(para->event.missing) +
                " transition_peak_periods=" +
                std::to_string(para->event.transitionPeakPeriods);
        }
        LOG(message, INFO);
        LOG(debugMessage, DEBUG);
        if (prepared)
            setRuntimeState(*para, RuntimeState::EventReady);
        else
            setPassiveRuntimeState(*para);
    } catch (const std::exception &error) {
        // Publish any partially started freezes before releasing their banks.
        try {
            for (size_t i = finished; i < begun; ++i)
                para->receiveBuffers[i]->finishFreeze();
            for (size_t i = 0; i < begun; ++i)
                para->receiveBuffers[i]->release();
        } catch (const std::exception &cleanup) {
            LOG(cleanup.what(), FATAL);
            para->failed = true;
            para->flags->threadStop();
        }
        std::string message;
        {
            std::lock_guard<std::mutex> lock(para->eventMutex);
            para->event.state = EventState::Rejected;
            para->event.error = error.what();
            remember(*para);
            message = "Event rejected: " + para->event.error;
        }
        LOG(message, WARN);
        if (para->failed)
            setRuntimeState(*para, RuntimeState::Error);
        else
            setPassiveRuntimeState(*para);
    }
}
// Validate control requests and advance node event state; allow RELEASE only after persistence is confirmed.
std::string ProcessControl(CacheRuntime &para, const std::string &request) {
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
            uint64_t transitionUsed = 0;
            uint64_t transitionCapacity = 0;
            uint64_t lastFreezeMilliseconds = 0;
            uint64_t receivedPackets = 0;
            uint64_t lostPackets = 0;
            uint64_t lossPeriods = 0;
            uint64_t lastLossPeriod = 0;
            uint64_t latePackets = 0;
            uint64_t lastPacketAgeMilliseconds = 0;
            unsigned receivingPorts = 0;
            std::string lostByPort = "{";
            std::string inactivePorts = "[";
            bool transitionWarning = false;
            bool ready = para.runtimeState.load() == RuntimeState::CachingData &&
                         !isActiveEventState(para.event.state) && !para.failed;
            for (size_t index = 0; index < para.receiveBuffers.size(); ++index) {
                auto s = para.receiveBuffers[index]->status();
                oldest = std::max(oldest, s.oldest);
                end = std::min(end, s.settledEnd);
                transitionUsed = std::max(transitionUsed, s.transitionUsed);
                transitionCapacity = std::max(transitionCapacity, s.transitionCapacity);
                lastFreezeMilliseconds =
                    std::max(lastFreezeMilliseconds, s.lastFreezeMilliseconds);
                transitionWarning = transitionWarning || s.transitionWarning;
                receivedPackets += s.received;
                lostPackets += s.lost;
                lossPeriods += s.lossPeriods;
                lastLossPeriod = std::max(lastLossPeriod, s.lastLossPeriod);
                latePackets += s.late;
                if (s.hasData)
                    lastPacketAgeMilliseconds =
                        std::max(lastPacketAgeMilliseconds, s.lastPacketAgeMilliseconds);
                if (s.hasData &&
                    s.lastPacketAgeMilliseconds <= DATA_FLOW_TIMEOUT_MILLISECONDS) {
                    ++receivingPorts;
                } else {
                    if (inactivePorts.size() > 1)
                        inactivePorts += ',';
                    inactivePorts += std::to_string(index);
                }
                if (s.lost) {
                    if (lostByPort.size() > 1)
                        lostByPort += ',';
                    lostByPort += quoteJson(std::to_string(index)) + ':' +
                                  std::to_string(s.lost);
                }
                ready = ready && s.ready;
            }
            lostByPort += '}';
            inactivePorts += ']';
            if (para.receiveBuffers.empty())
                end = 0;
            ready = ready && para.initializedPorts == para.receiveBuffers.size();
            const char *status = "WARMING";
            if (para.failed)
                status = "ERROR";
            else if (ready)
                status = "READY";
            else if (isActiveEventState(para.event.state))
                status = "BUSY";
            const char *dataFlow =
                (!para.receiveBuffers.empty() &&
                 receivingPorts == para.receiveBuffers.size()) ? "RECEIVING" :
                (receivingPorts ? "PARTIAL" : "NO_DATA");
            return reply("\"status\":" + quoteJson(status) +
                         ",\"runtime_state\":" +
                         quoteJson(runtimeStateName(para.runtimeState.load())) +
                         ",\"runtime_state_age_ms\":" +
                         std::to_string(runtimeStateAgeMilliseconds(para)) +
                         ",\"uptime_ms\":" +
                         std::to_string(processUptimeMilliseconds(para)) +
                         ",\"accept_trigger\":" + (ready ? "true" : "false") +
                         ",\"oldest_period\":" + std::to_string(oldest) + ",\"settled_end\":" +
                         std::to_string(end) + ",\"event_id\":" + quoteJson(para.event.id) +
                         ",\"event_state\":" + quoteJson(eventStateName(para.event.state)) +
                         ",\"initialized_ports\":" + std::to_string(para.initializedPorts.load()) +
                         ",\"receive_ports\":" +
                         std::to_string(para.receiveBuffers.size()) +
                         ",\"data_flow\":" + quoteJson(dataFlow) +
                         ",\"receiving_ports\":" + std::to_string(receivingPorts) +
                         ",\"last_packet_age_ms\":" +
                         std::to_string(lastPacketAgeMilliseconds) +
                         ",\"inactive_ports\":" + inactivePorts +
                         ",\"received_packets\":" + std::to_string(receivedPackets) +
                         ",\"lost_packets\":" + std::to_string(lostPackets) +
                         ",\"loss_periods\":" + std::to_string(lossPeriods) +
                         ",\"last_loss_period\":" + std::to_string(lastLossPeriod) +
                         ",\"late_packets\":" + std::to_string(latePackets) +
                         ",\"lost_by_port\":" + lostByPort +
                         ",\"ignored_packets\":" + std::to_string(para.ignoredPackets.load()) +
                         ",\"transition_used_periods\":" +
                         std::to_string(transitionUsed) +
                         ",\"transition_capacity_periods\":" +
                         std::to_string(transitionCapacity) +
                         ",\"transition_warning\":" +
                         (transitionWarning ? "true" : "false") +
                         ",\"last_freeze_ms\":" +
                         std::to_string(lastFreezeMilliseconds));
        }
        if (command == "QUERY_PORT") {
            uint64_t p = getNumber(msg, "port");
            if (p >= para.receiveBuffers.size())
                throw std::runtime_error("INVALID_PORT");
            auto s = para.receiveBuffers[p]->status();
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
                         ",\"lost_packets\":" + std::to_string(s.lost) +
                         ",\"loss_periods\":" + std::to_string(s.lossPeriods) +
                         ",\"last_loss_period\":" + std::to_string(s.lastLossPeriod) +
                         ",\"duplicate_packets\":" + std::to_string(s.duplicate) +
                         ",\"late_packets\":" + std::to_string(s.late) +
                         ",\"conflicting_packets\":" + std::to_string(s.conflicting) +
                         ",\"transition_capacity_periods\":" +
                         std::to_string(s.transitionCapacity) +
                         ",\"transition_used_periods\":" +
                         std::to_string(s.transitionUsed) +
                         ",\"transition_peak_periods\":" +
                         std::to_string(s.transitionPeak) +
                         ",\"transition_warning\":" +
                         (s.transitionWarning ? "true" : "false") +
                         ",\"last_freeze_ms\":" +
                         std::to_string(s.lastFreezeMilliseconds) +
                         ",\"freeze_count\":" + std::to_string(s.freezeCount));
        }
        auto id = getId(msg, "event_id");
        if (command == "PREPARE" || command == "CAPTURE") {
            if (command == "CAPTURE")
                throw std::runtime_error("USE_CONTROLLER_FOR_CAPTURE");
            uint64_t trace = getNumber(msg, "start.trace_period_id");
            uint64_t integration = getNumber(msg, "start.integration_period_id");
            uint64_t count = getNumber(msg, "period_count");
            uint64_t channels = hasField(msg, "channel_count") ? getNumber(msg, "channel_count") : 384;
            const unsigned modeNodes = channels == 288 ? 11U : 14U;
            const uint64_t modeMask = (uint64_t(1) << modeNodes) - 1;
            uint64_t serverMask = hasField(msg, "server_mask") ? getNumber(msg, "server_mask") : modeMask;
            uint64_t epochEnd = (1ULL << 20) * para.config->integrationPerTrace;
            if (channels != 288 && channels != 384)
                throw std::runtime_error("INVALID_CHANNEL_COUNT");
            if (channels == 288 && para.config->serverId >= 11)
                throw std::runtime_error("CHANNEL_MODE_EXCLUDES_NODE");
            if (!serverMask || (serverMask & ~modeMask) ||
                !(serverMask & (uint64_t(1) << para.config->serverId)))
                throw std::runtime_error("INVALID_SERVER_SELECTION");
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
                if (existing->start != start || existing->count != count ||
                    existing->channelCount != channels || existing->serverMask != serverMask)
                    throw std::runtime_error("EVENT_ID_CONFLICT");
                return reply(eventJson(*existing));
            }
            if (para.failed || isActiveEventState(para.event.state))
                throw std::runtime_error("BUSY");
            if (para.runtimeState.load() != RuntimeState::CachingData)
                throw std::runtime_error("NOT_READY");
            if (para.initializedPorts != para.receiveBuffers.size())
                throw std::runtime_error("NOT_READY");
            for (const auto &pool : para.receiveBuffers) {
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
            para.event.channelCount = static_cast<unsigned>(channels);
            para.event.serverMask = static_cast<unsigned>(serverMask);
            para.event.start = start;
            para.event.count = count;
            para.event.state = EventState::Preparing;
            setRuntimeState(para, RuntimeState::PreparingEvent);
            try {
                para.eventThread = std::thread(prepareEvent, &para);
            } catch (...) {
                para.event.state = EventState::Rejected;
                para.event.error = "prepare thread creation failed";
                remember(para);
                setPassiveRuntimeState(para);
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
                cancelled.state = EventState::Cancelled;
                rememberEvent(para, cancelled);
                return reply(eventJson(cancelled));
            }
            throw std::runtime_error("EVENT_NOT_FOUND");
        }
        auto &e = para.event;
        if (command == "QUERY_EVENT")
            return reply(eventJson(e));
        if (command == "STORE") {
            if (e.state == EventState::Prepared || e.state == EventState::SendFailed) {
                if (para.eventThread.joinable())
                    para.eventThread.join();
                e.state = EventState::Sending;
                e.sentBytes = 0;
                e.sendMilliseconds = 0;
                e.sendRateBitsPerSecond = 0;
                e.error.clear();
                setRuntimeState(para, RuntimeState::SendingCachedData);
                try {
                    para.eventThread = std::thread(InitSendStoreThread, &para);
                } catch (...) {
                    e.state = EventState::SendFailed;
                    e.error = "send thread creation failed";
                    setRuntimeState(para, RuntimeState::SendFailed);
                    throw;
                }
            } else if (e.state != EventState::Sending && e.state != EventState::Stored &&
                       e.state != EventState::Released)
                throw std::runtime_error("NOT_PREPARED");
        } else if (command == "RELEASE") {
            if (e.state == EventState::Stored) {
                for (auto &pool : para.receiveBuffers)
                    pool->release();
                e.state = EventState::Released;
                remember(para);
                setPassiveRuntimeState(para);
            } else if (e.state != EventState::Released)
                throw std::runtime_error("NOT_DURABLY_STORED");
        } else if (command == "CANCEL") {
            if (e.state == EventState::Preparing)
                e.cancelRequested = true;
            else if (e.state == EventState::Prepared || e.state == EventState::SendFailed) {
                for (auto &pool : para.receiveBuffers)
                    pool->release();
                e.state = EventState::Cancelled;
                remember(para);
                setPassiveRuntimeState(para);
            } else if (e.state != EventState::Cancelled && e.state != EventState::Rejected)
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
