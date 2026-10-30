#include "zmq_store.h"
#include <algorithm>
#include <arpa/inet.h>
#include <cerrno>
#include <cstring>
#include <ifaddrs.h>
#include <stdexcept>
#include <zmq.h>

namespace {
constexpr size_t MAX_MESSAGE = 13 * 1024 * 1024;
constexpr size_t MAX_RESULT_JSON = 1200;

uint32_t read32(const unsigned char *p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}

std::runtime_error zmqError(const char *operation) {
    return std::runtime_error(std::string(operation) + ": " + zmq_strerror(zmq_errno()));
}

void setIntOption(void *socket, int option, int value) {
    if (zmq_setsockopt(socket, option, &value, sizeof(value)) != 0)
        throw zmqError("ZeroMQ socket option failed");
}

void validateSource(const SendPortConfig &config) {
    in_addr source{}, destination{};
    if (inet_pton(AF_INET, config.sourceIp.c_str(), &source) != 1 ||
        inet_pton(AF_INET, config.destinationIp.c_str(), &destination) != 1 ||
        source.s_addr == INADDR_ANY || destination.s_addr == INADDR_ANY)
        throw std::runtime_error("ZeroMQ source and destination must be specific IPv4 addresses");
    ifaddrs *addresses = nullptr;
    if (getifaddrs(&addresses) != 0)
        throw std::runtime_error(std::string("cannot inspect local interfaces: ") + strerror(errno));
    bool found = false;
    for (auto *entry = addresses; entry; entry = entry->ifa_next) {
        if (!entry->ifa_addr || entry->ifa_addr->sa_family != AF_INET ||
            (config.ethName != "-" && config.ethName != entry->ifa_name))
            continue;
        const auto *address = reinterpret_cast<const sockaddr_in *>(entry->ifa_addr);
        if (address->sin_addr.s_addr == source.s_addr) {
            found = true;
            break;
        }
    }
    freeifaddrs(addresses);
    if (!found)
        throw std::runtime_error("ZeroMQ source IP is not assigned to the configured interface");
}

void waitSocket(void *socket, short events, PARAMETERS &para,
                std::chrono::steady_clock::time_point deadline) {
    while (para.flags->ifControl) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline)
            throw std::runtime_error("ZeroMQ socket timed out; event remains protected");
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
        const long milliseconds = static_cast<long>(std::max<int64_t>(1, std::min<int64_t>(100, remaining.count())));
        zmq_pollitem_t item{socket, 0, events, 0};
        const int rc = zmq_poll(&item, 1, milliseconds);
        if (rc < 0) {
            if (zmq_errno() == EINTR)
                continue;
            throw zmqError("ZeroMQ poll failed");
        }
        if (rc > 0 && (item.revents & events))
            return;
    }
    throw std::runtime_error("ZeroMQ transfer interrupted; event remains protected");
}

struct Message {
    zmq_msg_t value;
    Message() {
        if (zmq_msg_init(&value) != 0)
            throw zmqError("ZeroMQ message init failed");
    }
    ~Message() { zmq_msg_close(&value); }
};
} // namespace

// Limit the average send rate by application message size and allow a stop signal to interrupt the wait.
void StorePacer::wait(size_t bytes, const ThreadFlags *flags) {
    // A complete ZeroMQ message is queued at once. This limits average
    // application bytes, not short bursts, TCP retransmissions or link overhead.
    std::lock_guard<std::mutex> guard(mutex);
    auto now = std::chrono::steady_clock::now();
    while (next > now) {
        if (flags && !flags->ifControl)
            throw std::runtime_error("ZeroMQ transfer interrupted; event remains protected");
        const auto remaining = next - now;
        if (remaining > std::chrono::milliseconds(50))
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        else if (remaining > std::chrono::microseconds(100))
            std::this_thread::sleep_until(next - std::chrono::microseconds(50));
        now = std::chrono::steady_clock::now();
    }
    next = now + std::chrono::nanoseconds((bytes * 8000000000ULL + rate - 1) / rate);
}

// Validate the source interface and address, then create a bounded, timeout-enabled ZeroMQ request connection.
StoreConnection::StoreConnection(PARAMETERS &p, unsigned lane, StorePacer &limit)
    : para(p), pacer(limit) {
    const auto &config = para.config->sendPorts[lane];
    validateSource(config);
    context = zmq_ctx_new();
    if (!context)
        throw zmqError("ZeroMQ context creation failed");
    try {
        socket = zmq_socket(context, ZMQ_REQ);
        if (!socket)
            throw zmqError("ZeroMQ REQ socket creation failed");
        setIntOption(socket, ZMQ_LINGER, 0);
        setIntOption(socket, ZMQ_IMMEDIATE, 1);
        setIntOption(socket, ZMQ_SNDHWM, 1);
        setIntOption(socket, ZMQ_RCVHWM, 1);
        setIntOption(socket, ZMQ_SNDTIMEO, static_cast<int>(para.config->socketTimeoutSeconds * 1000));
        setIntOption(socket, ZMQ_RCVTIMEO, static_cast<int>(para.config->socketTimeoutSeconds * 1000));
        int64_t maxReply = MAX_RESULT_JSON + 4;
        if (zmq_setsockopt(socket, ZMQ_MAXMSGSIZE, &maxReply, sizeof(maxReply)) != 0)
            throw zmqError("ZeroMQ reply size limit failed");
        if (config.ethName != "-") {
#ifdef ZMQ_BINDTODEVICE
            if (zmq_setsockopt(socket, ZMQ_BINDTODEVICE, config.ethName.data(), config.ethName.size()) != 0)
                throw zmqError("ZeroMQ interface binding failed");
#else
            throw std::runtime_error("libzmq does not support required interface binding");
#endif
        }
        // ZeroMQ's TCP source endpoint binds the source IPv4 and an ephemeral port.
        const std::string endpoint = "tcp://" + config.sourceIp + ":0;" + config.destinationIp + ":" +
                                     std::to_string(config.destinationPort);
        if (zmq_connect(socket, endpoint.c_str()) != 0)
            throw zmqError("ZeroMQ connect failed");
    } catch (...) {
        if (socket)
            zmq_close(socket);
        zmq_ctx_term(context);
        socket = nullptr;
        context = nullptr;
        throw;
    }
}

StoreConnection::~StoreConnection() {
    if (socket)
        zmq_close(socket);
    if (context)
        zmq_ctx_term(context);
}

// Send one complete message and wait for its reply; parse the STORED result separately for the final message.
void StoreConnection::sendMessage(const std::vector<unsigned char> &message, bool final,
                                  bool allowStored) {
    if (message.empty() || message.size() > MAX_MESSAGE)
        throw std::runtime_error("ZeroMQ message size invalid");
    pacer.wait(message.size(), para.flags);
    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::seconds(para.config->socketTimeoutSeconds);
    while (para.flags->ifControl) {
        const int sent = zmq_send(socket, message.data(), message.size(), ZMQ_DONTWAIT);
        if (sent >= 0) {
            if (static_cast<size_t>(sent) != message.size())
                throw std::runtime_error("ZeroMQ message send was incomplete");
            break;
        }
        if (zmq_errno() == EINTR)
            continue;
        if (zmq_errno() == EAGAIN) {
            waitSocket(socket, ZMQ_POLLOUT, para, deadline);
            continue;
        }
        throw zmqError("ZeroMQ send failed");
    }
    if (!para.flags->ifControl)
        throw std::runtime_error("ZeroMQ transfer interrupted; event remains protected");
    // zmq_send queues the whole block; the receiver cannot acknowledge until
    // it has traversed the network and been written. Allow for configured
    // slow transfer rates in addition to the disk/feedback timeout.
    const uint64_t transferSeconds =
        (message.size() * 8ULL + para.config->sendRateBitsPerSecond - 1) /
        para.config->sendRateBitsPerSecond;
    deadline = std::chrono::steady_clock::now() +
               std::chrono::seconds(para.config->socketTimeoutSeconds + 2 * transferSeconds);
    Message reply;
    while (para.flags->ifControl) {
        const int received = zmq_msg_recv(&reply.value, socket, ZMQ_DONTWAIT);
        if (received >= 0) {
            if (zmq_msg_more(&reply.value))
                throw std::runtime_error("ZeroMQ multipart reply is invalid");
            const auto *data = static_cast<const unsigned char *>(zmq_msg_data(&reply.value));
            if (received)
                result.assign(data, data + received);
            else
                result.clear();
            if (result.size() >= 6 && std::equal(result.begin(), result.begin() + 6, "ERROR:"))
                throw std::runtime_error("storage receiver rejected message: " +
                                         std::string(result.begin() + 6, result.end()));
            if (!final && result != std::vector<unsigned char>{'O', 'K'} && !allowStored)
                throw std::runtime_error("invalid ZeroMQ block acknowledgment");
            if (!final && result == std::vector<unsigned char>{'O', 'K'})
                result.clear();
            return;
        }
        if (zmq_errno() == EINTR)
            continue;
        if (zmq_errno() == EAGAIN) {
            waitSocket(socket, ZMQ_POLLIN, para, deadline);
            continue;
        }
        throw zmqError("ZeroMQ receive failed");
    }
    throw std::runtime_error("ZeroMQ transfer interrupted; event remains protected");
}

std::string StoreConnection::resultJson() const {
    if (result.size() < 5 || read32(result.data()) != result.size() - 4 ||
        result.size() > MAX_RESULT_JSON + 4)
        throw std::runtime_error("invalid ZeroMQ durable acknowledgment");
    return std::string(result.begin() + 4, result.end());
}
