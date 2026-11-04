#include "functions.h"
#include "log_output.h"
#include <csignal>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
static void Prepare(CacheRuntime &para);
static void Finish(CacheRuntime &para);
static volatile std::sig_atomic_t stopped = 0;
static void stopHandler(int) {
    stopped = 1;
}
// Load and validate the configuration, allocate buffers, and start the receive threads and control loop.
int StoreMain(int argc, char **argv, StoreMode mode) {
    ThreadFlags flags;
    CacheRuntime para;
    para.flags = &flags;
    std::thread signalThread;
    try {
        if (argc < 2) {
            std::cerr << "Usage: " << argv[0]
                      << " server_XX.yaml [receive_config.yaml] [--check-config|--simulate]\n";
            return 2;
        }
        const std::string recvFile = argc > 2 ? argv[2] : "receive_config.yaml";
        bool check = false;
        for (int i = 3; i < argc; ++i) {
            std::string option = argv[i];
            if (option == "--check-config")
                check = true;
            else if (option == "--simulate")
                para.simulation = true;
            else
                throw std::runtime_error("unknown command-line argument");
        }
        YamlConfig config(recvFile, argv[1], mode);
        para.config = &config;
        std::cout << "server=" << config.serverId << " receivePorts=" << config.receivePorts.size()
                  << " estimatedBytes=" << config.allocationBytes()
                  << " transitionPeriods=" << config.transitionPeriods() << "\n";
        std::cout << "transport=ZeroMQ sendRateBitsPerSecond=" << config.sendRateBitsPerSecond << "\n";
        for (unsigned p = 0; p < config.receivePorts.size(); ++p) {
            const auto &port = config.receivePorts[p];
            std::cout << "port=" << p << " packetId=" << port.packetId
                      << " boards=" << port.boardBegin << ".."
                      << port.boardBegin + port.boardNum - 1 << " device=" << port.deviceName;
            if (port.packetsPerBoard > 1)
                std::cout << " packetIdEnd=" << port.packetId + port.packetsPerBoard - 1
                          << " packetsPerFrame=" << port.packetSlots()
                          << " frequencyBegin=" << port.frequencyBegin
                          << " frequencyCount=" << port.frequencyCount
                          << " validFrequencyCount=" << port.validFrequencyCount;
            std::cout << "\n";
        }
        if (check)
            return 0;
        if (!para.simulation) {
            if (config.acquisitionId.find("CHANGE_ME") != std::string::npos)
                throw std::runtime_error("set the shared acquisitionId before running");
            for (const auto &port : config.receivePorts)
                if (port.deviceName.find("CHANGE_ME") != std::string::npos)
                    throw std::runtime_error(
                        "configure actual receive PCI addresses before running");
            for (const auto &port : config.sendPorts)
                if (port.ethName == "-" || port.ethName.find("CHANGE_ME") != std::string::npos ||
                    port.sourceIp == "0.0.0.0" || port.destinationIp == "0.0.0.0")
                    throw std::runtime_error("configure dedicated send interfaces before running");
        }
        initLogOut(config.logFilePath);
        LOG("Cache data is memory-only; a process or host restart discards all buffered events.",
            WARN);
        std::signal(SIGINT, stopHandler);
        std::signal(SIGTERM, stopHandler);
        Prepare(para);
        // Config must outlive all threads, including exceptional shutdown.
        try {
            signalThread = std::thread([&]() {
                while (flags.ifControl && !stopped)
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                flags.threadStop();
            });
            for (unsigned p = 0; p < config.receivePorts.size(); ++p)
                para.receiveThreads.emplace_back(InitRecvAntennaThread, &para, p);
            InitControlThread(&para);
        } catch (...) {
            Finish(para);
            if (signalThread.joinable())
                signalThread.join();
            throw;
        }
        Finish(para);
        if (signalThread.joinable())
            signalThread.join();
        return para.failed ? 1 : 0;
    } catch (const std::exception &e) {
        flags.threadStop();
        if (signalThread.joinable())
            signalThread.join();
        std::cerr << "[FATAL] " << e.what() << "\n";
        return 1;
    }
}

// Initialise and stop the receive workers alongside the application lifecycle.
static uint64_t availableMemory() {
    std::ifstream in("/proc/meminfo");
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string key;
        uint64_t kib;
        if (row >> key >> kib && key == "MemAvailable:")
            return kib * 1024;
    }
    throw std::runtime_error("cannot read MemAvailable");
}

// Check available memory and create double buffers plus a transition area for each receive port.
static void Prepare(CacheRuntime &para) {
    const auto &c = *para.config;
    if (c.allocationBytes() + c.reserveMemoryBytes > availableMemory())
        throw std::runtime_error(
            "insufficient available RAM for A/B/transition and system reserve");
    LOG("Allocating " + std::to_string(c.allocationBytes()) + " bytes, no CUDA dependency.", START);
    for (unsigned index = 0; index < c.receivePorts.size(); ++index) {
        const auto &port = c.receivePorts[index];
        para.receiveBuffers.emplace_back(
            new ReceiveRingBuffer(index, c.halfPeriods(), c.transitionPeriods(),
                                  c.maxEventPeriods(), port.packetSlots(),
                                  c.inputDataBytes + INPUT_HEADER_BYTES, c.reorderPeriods,
                                  c.settleMilliseconds, c.transitionWarnPercent,
                                  c.prefaultMemory));
    }
}

static void Finish(CacheRuntime &para) {
    para.flags->threadStop();
    for (auto &thread : para.receiveThreads)
        if (thread.joinable())
            thread.join();
    if (para.eventThread.joinable())
        para.eventThread.join();
}
