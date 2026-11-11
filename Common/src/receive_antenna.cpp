#include "functions.h"
#include "log_output.h"
#include <arpa/inet.h>
#include <cerrno>
#include <cstring>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
#ifndef STORE_NO_IXCAP
#include <ixcap_lib.h>
#endif

static const unsigned BATCH_SIZE = 512;
static void logFirstUdpPacket(unsigned port, const unsigned char *data, size_t bytes,
                              size_t frameBytes, unsigned integrationPerTrace, bool &logged) {
    if (logged)
        return;
    logged = true;
    if (bytes < 8) {
        LOG("Receive port " + std::to_string(port) +
                " first UDP payload is too short: frame_bytes=" +
                std::to_string(frameBytes) + " payload_bytes=" + std::to_string(bytes),
            WARN);
        return;
    }
    const auto header = decodeInput(data);
    LOG("Receive port " + std::to_string(port) +
            " first captured UDP packet: packet_id=" + std::to_string(header.packetId) +
            " board=" + std::to_string(header.board) +
            " trace_period_id=" + std::to_string(header.trace) +
            " integration_period_id=" + std::to_string(header.integration) +
            " absolute_period=" +
            std::to_string(header.absolute(integrationPerTrace)) +
            " valid=" + std::to_string(header.valid) +
            " frame_bytes=" + std::to_string(frameBytes) +
            " payload_bytes=" + std::to_string(bytes),
        INFO);
}
// Validate a raw payload packet and write it to the matching receive-port buffer by absolute integration period and board position.
static void receivePacket(CacheRuntime *para, unsigned p, const unsigned char *data, size_t bytes,
                          bool &firstPacketLogged) {
    const auto &cfg = *para->config;
    const auto &port = cfg.receivePorts[p];
    if (bytes != INPUT_HEADER_BYTES + cfg.inputDataBytes) {
        ++para->ignoredPackets;
        return;
    }
    InputHeader h = decodeInput(data);
    if (h.valid != 15 || h.packetId < port.packetId ||
        h.packetId >= port.packetId + port.packetsPerBoard || h.board < port.boardBegin ||
        h.board >= port.boardBegin + port.boardNum || h.integration >= cfg.integrationPerTrace) {
        ++para->ignoredPackets;
        return;
    }
    const unsigned slot =
        (h.board - port.boardBegin) * port.packetsPerBoard + h.packetId - port.packetId;
    const uint64_t absolutePeriod = h.absolute(cfg.integrationPerTrace);
    if (para->receiveBuffers[p]->receive(absolutePeriod, slot, h.ifTrace, data)) {
        if (!firstPacketLogged) {
            noteAcceptedPacket(*para);
            firstPacketLogged = true;
            LOG("Receive port " + std::to_string(p) +
                " first accepted UDP packet: packet_id=" + std::to_string(h.packetId) +
                " board=" + std::to_string(h.board) +
                " trace_period_id=" + std::to_string(h.trace) +
                " integration_period_id=" + std::to_string(h.integration) +
                " absolute_period=" + std::to_string(absolutePeriod),
                INFO);
        }
    }
}
static void simulationReceive(CacheRuntime *para, unsigned p) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        throw std::runtime_error("simulation socket failed");
    struct Close {
        int fd;
        ~Close() {
            close(fd);
        }
    } closeFd{fd};
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(para->config->receivePorts[p].simulationPort);
    if (bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)))
        throw std::runtime_error("simulation bind failed");
    int buffer = 8 << 20;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &buffer, sizeof(buffer));
    ++para->initializedPorts;
    unsigned char packet[16384];
    bool firstUdpLogged = false;
    bool firstPacketLogged = false;
    while (para->flags->ifRecvAntenna) {
        pollfd poller{fd, POLLIN, 0};
        if (poll(&poller, 1, 100) <= 0)
            continue;
        auto n = recv(fd, packet, sizeof(packet), 0);
        if (n >= 0) {
            logFirstUdpPacket(p, packet, size_t(n), size_t(n),
                              para->config->integrationPerTrace, firstUdpLogged);
            receivePacket(para, p, packet, size_t(n), firstPacketLogged);
        }
    }
}
// Run each receive port independently, receive packets in batches after NIC initialization, and stop all threads on error.
void InitRecvAntennaThread(CacheRuntime *para, unsigned p) {
    try {
        int cpu = para->config->receivePorts[p].cpuId;
        if (cpu >= 0) {
            cpu_set_t cpus;
            CPU_ZERO(&cpus);
            CPU_SET(cpu, &cpus);
            if (pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus))
                throw std::runtime_error("receive CPU affinity failed");
        }
        LOG("Receive port " + std::to_string(p) +
                " thread: device=" + para->config->getDeviceName(p) +
                " cpu_id=" + std::to_string(cpu) +
                " simulation=" + (para->simulation ? "true" : "false"),
            DEBUG);
        if (para->simulation) {
            simulationReceive(para, p);
            return;
        }
#ifdef STORE_NO_IXCAP
        throw std::runtime_error("this binary was built without ixcap; use --simulate");
#else
        // Initialize ixcap serially: global library initialization is not assumed thread-safe.
        static std::mutex initMutex;
        ixcap_dev *capture;
        {
            std::lock_guard<std::mutex> lock(initMutex);
            ixcap_set_pkt_entry_size(16384);
            capture = ixcap_init(para->config->getDeviceName(p).c_str(), 1, 1);
        }
        if (!capture)
            throw std::runtime_error("ixcap_init failed on port " + std::to_string(p));
        ixcap_pkt_buf *packets[BATCH_SIZE];
        bool firstUnparsedFrameLogged = false;
        bool firstUdpLogged = false;
        bool firstPacketLogged = false;
        ++para->initializedPorts;
        LOG("Receive port " + std::to_string(p) +
                " packetId=" + std::to_string(para->config->receivePorts[p].packetId) + ".." +
                std::to_string(para->config->receivePorts[p].packetId +
                               para->config->receivePorts[p].packetsPerBoard - 1),
            START);
        while (para->flags->ifRecvAntenna) {
            uint32_t count = ixcap_recv_batch(capture, 0, packets, BATCH_SIZE);
            for (uint32_t i = 0; i < count; ++i) {
                const auto *raw = static_cast<unsigned char *>(ixcap_get_pkt_data(packets[i]));
                const unsigned char *data;
                size_t bytes;
                try {
                    const auto frameBytes = ixcap_get_pkt_size(packets[i]);
                    if (inputPayload(raw, frameBytes, data, bytes)) {
                        logFirstUdpPacket(p, data, bytes, frameBytes,
                                          para->config->integrationPerTrace, firstUdpLogged);
                        receivePacket(para, p, data, bytes, firstPacketLogged);
                    } else {
                        ++para->ignoredPackets;
                        if (!firstUnparsedFrameLogged) {
                            firstUnparsedFrameLogged = true;
                            LOG("Receive port " + std::to_string(p) +
                                    " captured a frame but Ethernet/IPv4/UDP parsing failed: "
                                    "frame_bytes=" + std::to_string(frameBytes),
                                WARN);
                        }
                    }
                } catch (...) {
                    for (uint32_t j = i; j < count; ++j)
                        ixcap_free_pkt_buf(packets[j]);
                    throw;
                }
                ixcap_free_pkt_buf(packets[i]);
            }
        }
#endif
    } catch (const std::exception &e) {
        LOG(e.what(), FATAL);
        para->failed = true;
        setRuntimeState(*para, RuntimeState::Error);
        para->flags->threadStop();
    }
}
