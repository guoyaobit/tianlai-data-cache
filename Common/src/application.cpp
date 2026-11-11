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
    std::thread startupThread;
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
                  << " transitionPeriods=" << config.transitionPeriods()
                  << " debug=" << (config.debug ? "true" : "false") << "\n";
        std::cout << "transport=ZeroMQ lanes=2 sendInterface=" << config.sendLink.ethName
                  << " rateLimit=none\n";
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
        if (para.simulation) {
            for (const auto &port : config.receivePorts)
                if (!port.simulationPort)
                    throw std::runtime_error(
                        "set simulationPort for every receive port when using --simulate");
        } else {
            for (const auto &port : config.receivePorts)
                if (port.deviceName.find("CHANGE_ME") != std::string::npos)
                    throw std::runtime_error(
                        "configure actual receive PCI addresses before running");
            const auto &link = config.sendLink;
            if (link.ethName == "-" || link.ethName.find("CHANGE_ME") != std::string::npos ||
                link.sourceIp == "0.0.0.0" || link.destinationIp == "0.0.0.0")
                throw std::runtime_error("configure the storage send interface before running");
        }
        initLogOut(config.logFilePath, config.debug);
        LOG("Runtime config: server=" + std::to_string(config.serverId) +
                " receive_ports=" + std::to_string(config.receivePorts.size()) +
                " half_periods=" + std::to_string(config.halfPeriods()) +
                " transition_periods=" + std::to_string(config.transitionPeriods()) +
                " reorder_periods=" + std::to_string(config.reorderPeriods) +
                " prefault=" + (config.prefaultMemory ? "true" : "false"),
            DEBUG);
        LOG("Cache data is memory-only; a process or host restart discards all buffered events.",
            WARN);
        std::signal(SIGINT, stopHandler);
        std::signal(SIGTERM, stopHandler);
        // Config must outlive all threads, including exceptional shutdown.
        try {
            signalThread = std::thread([&]() {
                while (flags.ifControl && !stopped)
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                flags.threadStop();
            });
            setRuntimeState(para, RuntimeState::PreparingMemory);
            startupThread = std::thread([&]() {
                try {
                    Prepare(para);
                    if (!flags.ifControl)
                        return;
                    setRuntimeState(para, RuntimeState::WaitingForData);
                    for (unsigned p = 0; p < config.receivePorts.size(); ++p)
                        para.receiveThreads.emplace_back(InitRecvAntennaThread, &para, p);
                } catch (const std::exception &error) {
                    LOG(error.what(), FATAL);
                    para.failed = true;
                    setRuntimeState(para, RuntimeState::Error);
                    flags.threadStop();
                }
            });
            InitControlThread(&para);
        } catch (...) {
            flags.threadStop();
            if (startupThread.joinable())
                startupThread.join();
            Finish(para);
            if (signalThread.joinable())
                signalThread.join();
            throw;
        }
        if (startupThread.joinable())
            startupThread.join();
        Finish(para);
        if (signalThread.joinable())
            signalThread.join();
        return para.failed ? 1 : 0;
    } catch (const std::exception &e) {
        if (para.config)
            setRuntimeState(para, RuntimeState::Error);
        flags.threadStop();
        if (startupThread.joinable())
            startupThread.join();
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
    std::vector<std::unique_ptr<ReceiveRingBuffer>> buffers;
    buffers.reserve(c.receivePorts.size());
    for (unsigned index = 0; index < c.receivePorts.size(); ++index) {
        const auto &port = c.receivePorts[index];
        LOG("Receive port " + std::to_string(index) +
                " buffer: packet_slots=" + std::to_string(port.packetSlots()) +
                " physical_boards=" + std::to_string(port.boardNum) +
                " packets_per_board=" + std::to_string(port.packetsPerBoard) +
                " frame_bytes=" +
                std::to_string(port.packetSlots() *
                               uint64_t(c.inputDataBytes + INPUT_HEADER_BYTES)),
            DEBUG);
        buffers.emplace_back(
            new ReceiveRingBuffer(index, c.halfPeriods(), c.transitionPeriods(),
                                  c.maxEventPeriods(), port.packetSlots(),
                                  port.packetsPerBoard, c.inputDataBytes + INPUT_HEADER_BYTES,
                                  c.reorderPeriods,
                                  c.settleMilliseconds, c.transitionWarnPercent,
                                  c.prefaultMemory));
    }
    std::lock_guard<std::mutex> lock(para.eventMutex);
    para.receiveBuffers = std::move(buffers);
}

static void Finish(CacheRuntime &para) {
    setRuntimeState(para, RuntimeState::Stopping);
    para.flags->threadStop();
    for (auto &thread : para.receiveThreads)
        if (thread.joinable())
            thread.join();
    if (para.eventThread.joinable())
        para.eventThread.join();
}
