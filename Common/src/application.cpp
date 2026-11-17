#include "functions.h"
#include "log_output.h"
#include <algorithm>
#include <csignal>
#include <exception>
#include <fstream>
#include <iostream>
#include <map>
#include <pthread.h>
#include <sched.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
static void Prepare(CacheRuntime &para);
static void Finish(CacheRuntime &para);
static void isolateWorkerCpus(const YamlConfig &config);
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
                  << " sendCpu0=" << config.sendLink.cpuIds[0]
                  << " sendCpu1=" << config.sendLink.cpuIds[1] << " rateLimit=none\n";
        for (unsigned p = 0; p < config.receivePorts.size(); ++p) {
            const auto &port = config.receivePorts[p];
            std::cout << "port=" << p << " packetId=" << port.packetId
                      << " boards=" << port.boardBegin << ".."
                      << port.boardBegin + port.boardNum - 1 << " device=" << port.deviceName
                      << " cpu=" << port.cpuId;
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
        // Background workers inherit this mask. Receive and lane workers later
        // replace it with their dedicated CPUs, so logging, control and event
        // management cannot run on packet capture or storage-send CPUs.
        isolateWorkerCpus(config);
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

static void isolateWorkerCpus(const YamlConfig &config) {
    cpu_set_t background;
    CPU_ZERO(&background);
    if (sched_getaffinity(0, sizeof(background), &background))
        throw std::runtime_error("cannot read process CPU affinity");
    std::vector<int> dedicated;
    for (const auto &port : config.receivePorts) {
        if (port.cpuId >= 0)
            dedicated.push_back(port.cpuId);
    }
    dedicated.push_back(config.sendLink.cpuIds[0]);
    dedicated.push_back(config.sendLink.cpuIds[1]);
    std::set<int> isolated;
    for (int cpu : dedicated) {
        if (cpu < 0 || cpu >= CPU_SETSIZE || !CPU_ISSET(cpu, &background))
            throw std::runtime_error("dedicated worker CPU is unavailable: " +
                                     std::to_string(cpu));
        if (isolated.count(cpu))
            throw std::runtime_error("dedicated workers share one physical CPU core");
        std::ifstream siblingsFile("/sys/devices/system/cpu/cpu" + std::to_string(cpu) +
                                   "/topology/thread_siblings_list");
        std::string siblings;
        if (!(siblingsFile >> siblings))
            throw std::runtime_error("cannot read CPU thread siblings for " +
                                     std::to_string(cpu));
        std::stringstream ranges(siblings);
        std::string range;
        while (std::getline(ranges, range, ',')) {
            const auto separator = range.find('-');
            const int first = std::stoi(range.substr(0, separator));
            const int last = separator == std::string::npos
                ? first : std::stoi(range.substr(separator + 1));
            if (first < 0 || last < first || last >= CPU_SETSIZE)
                throw std::runtime_error("invalid CPU thread sibling list");
            for (int sibling = first; sibling <= last; ++sibling)
                isolated.insert(sibling);
        }
    }
    for (int cpu : isolated)
        if (CPU_ISSET(cpu, &background))
            CPU_CLR(cpu, &background);
    bool available = false;
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
        available = available || CPU_ISSET(cpu, &background);
    if (!available)
        throw std::runtime_error("dedicated worker CPUs leave no CPU for background threads");
    if (pthread_setaffinity_np(pthread_self(), sizeof(background), &background))
        throw std::runtime_error("background CPU affinity failed");
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

// Resident shared slabs count against MemAvailable but need no new allocation.
static uint64_t reusableMemory(const YamlConfig &config) {
    if (config.sharedMemoryDirectory.empty())
        return 0;
    uint64_t reusable = 0;
    const size_t page = static_cast<size_t>(getpagesize());
    for (unsigned port = 0; port < config.receivePorts.size(); ++port) {
        const uint64_t bytes = (2 * config.halfPeriods() + config.transitionPeriods()) *
            config.receivePorts[port].packetSlots() *
            uint64_t(config.inputDataBytes + INPUT_HEADER_BYTES);
        const std::string path = config.sharedMemoryDirectory + "/server-" +
            std::to_string(config.serverId) + "-port-" + std::to_string(port) + ".bin";
        const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0)
            continue;
        struct stat info{};
        uint64_t marker[2]{};
        if (fstat(fd, &info) == 0 && info.st_size == static_cast<off_t>(page + bytes) &&
            pread(fd, marker, sizeof(marker), 0) == sizeof(marker) &&
            marker[0] == 0x544c434143484531ULL && marker[1] == bytes)
            reusable += bytes;
        close(fd);
    }
    return reusable;
}

static int cpuNumaNode(int cpu) {
    if (cpu < 0)
        return -1;
    for (int node = 0; node < CPU_SETSIZE; ++node) {
        const std::string path = "/sys/devices/system/cpu/cpu" + std::to_string(cpu) +
                                 "/node" + std::to_string(node);
        if (access(path.c_str(), F_OK) == 0)
            return node;
    }
    return -1;
}

static uint64_t numaNodeMemory(int node) {
    std::ifstream in("/sys/devices/system/node/node" + std::to_string(node) + "/meminfo");
    std::string field;
    while (in >> field) {
        if (field != "MemTotal:")
            continue;
        uint64_t kib = 0;
        if (in >> kib)
            return kib * 1024;
        break;
    }
    return 0;
}

static int backgroundCpuOnNode(int node) {
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed))
        return -1;
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
        if (CPU_ISSET(cpu, &allowed) && cpuNumaNode(cpu) == node)
            return cpu;
    return -1;
}

struct PortAllocationPlan {
    unsigned port;
    int receiveNode;
    int allocationNode;
    int allocationCpu;
};

// Keep receive workers local to their NICs. If all four NICs share one NUMA
// node, spread the large buffers over memory-bearing NUMA nodes to avoid
// exhausting one node during prefault. Allocation remains serial per node.
static std::vector<PortAllocationPlan> planPortAllocations(const YamlConfig &config) {
    std::vector<PortAllocationPlan> plans;
    plans.reserve(config.receivePorts.size());
    for (unsigned port = 0; port < config.receivePorts.size(); ++port) {
        const int node = cpuNumaNode(config.receivePorts[port].cpuId);
        plans.push_back({port, node, node, config.receivePorts[port].cpuId});
    }

    const bool fourPortsOnOneNode = plans.size() == 4 && plans.front().receiveNode >= 0 &&
        std::all_of(plans.begin(), plans.end(), [&](const PortAllocationPlan &plan) {
            return plan.receiveNode == plans.front().receiveNode;
        });
    if (!fourPortsOnOneNode)
        return plans;

    const int localNode = plans.front().receiveNode;
    std::vector<std::pair<int, int>> targets;
    if (numaNodeMemory(localNode))
        targets.emplace_back(localNode, plans.front().allocationCpu);
    for (int node = 0; node < CPU_SETSIZE; ++node) {
        if (node == localNode || !numaNodeMemory(node))
            continue;
        const int cpu = backgroundCpuOnNode(node);
        if (cpu >= 0)
            targets.emplace_back(node, cpu);
    }
    if (targets.size() < 2) {
        LOG("Four receive ports share NUMA" + std::to_string(plans.front().receiveNode) +
                "; buffer prefault will be serialized on that node",
            WARN);
        return plans;
    }
    if (targets.size() > plans.size())
        targets.resize(plans.size());

    for (unsigned index = 0; index < plans.size(); ++index) {
        const auto &target = targets[index % targets.size()];
        plans[index].allocationNode = target.first;
        plans[index].allocationCpu = target.first == plans[index].receiveNode
            ? config.receivePorts[plans[index].port].cpuId : target.second;
    }
    LOG("Four receive ports share NUMA" + std::to_string(plans.front().receiveNode) +
            "; buffers balanced across " + std::to_string(targets.size()) + " NUMA nodes",
        WARN);
    return plans;
}

// First-touch one large port buffer on the CPU selected by the NUMA plan.
static std::unique_ptr<ReceiveRingBuffer> allocatePortBuffer(
        unsigned index, const ReceivePortConfig &port, const YamlConfig &config,
        int allocationCpu) {
    if (allocationCpu >= 0) {
        if (allocationCpu >= CPU_SETSIZE)
            throw std::runtime_error("buffer allocation CPU ID is out of range");
        cpu_set_t cpus;
        CPU_ZERO(&cpus);
        CPU_SET(allocationCpu, &cpus);
        if (pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus))
            throw std::runtime_error(
                "buffer allocation CPU affinity failed for port " +
                std::to_string(index) + " cpu " + std::to_string(allocationCpu));
    }
    return std::unique_ptr<ReceiveRingBuffer>(new ReceiveRingBuffer(
        index, config.halfPeriods(), config.transitionPeriods(),
        config.maxEventPeriods(), port.packetSlots(), port.packetsPerBoard,
        config.inputDataBytes + INPUT_HEADER_BYTES, config.reorderPeriods,
        config.settleMilliseconds, config.transitionWarnPercent,
        config.prefaultMemory, config.serverId, config.sharedMemoryDirectory));
}

// Check available memory and create double buffers plus a transition area for each receive port.
static void Prepare(CacheRuntime &para) {
    const auto &c = *para.config;
    const auto allocationStarted = std::chrono::steady_clock::now();
    if (c.allocationBytes() + c.reserveMemoryBytes >
        availableMemory() + reusableMemory(c))
        throw std::runtime_error(
            "insufficient available RAM for A/B/transition and system reserve");
    LOG("Allocating " + std::to_string(c.allocationBytes()) + " bytes, no CUDA dependency.", START);
    const auto plans = planPortAllocations(c);
    std::vector<std::unique_ptr<ReceiveRingBuffer>> buffers(c.receivePorts.size());
    std::vector<std::exception_ptr> failures(c.receivePorts.size());
    std::map<int, std::vector<PortAllocationPlan>> groups;
    for (const auto &plan : plans)
        groups[plan.allocationNode].push_back(plan);
    std::vector<std::thread> workers;
    workers.reserve(groups.size());
    try {
        for (const auto &plan : plans) {
            const auto &port = c.receivePorts[plan.port];
            LOG("Receive port " + std::to_string(plan.port) +
                    " buffer: packet_slots=" + std::to_string(port.packetSlots()) +
                    " physical_boards=" + std::to_string(port.boardNum) +
                    " packets_per_board=" + std::to_string(port.packetsPerBoard) +
                    " receive_numa=" + std::to_string(plan.receiveNode) +
                    " allocation_numa=" + std::to_string(plan.allocationNode) +
                    " allocation_cpu=" + std::to_string(plan.allocationCpu) +
                    " frame_bytes=" +
                    std::to_string(port.packetSlots() *
                                   uint64_t(c.inputDataBytes + INPUT_HEADER_BYTES)),
                DEBUG);
        }
        for (const auto &entry : groups) {
            const auto group = entry.second;
            workers.emplace_back([&, group]() {
                for (const auto &plan : group) {
                    try {
                        const auto started = std::chrono::steady_clock::now();
                        LOG("RX" + std::to_string(plan.port) +
                                " memory start: numa=" + std::to_string(plan.allocationNode),
                            INFO);
                        buffers[plan.port] = allocatePortBuffer(
                            plan.port, c.receivePorts[plan.port], c, plan.allocationCpu);
                        const auto milliseconds =
                            std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - started).count();
                        LOG("RX" + std::to_string(plan.port) +
                                " memory ready: numa=" +
                                std::to_string(plan.allocationNode) +
                                " ms=" + std::to_string(milliseconds),
                            INFO);
                    } catch (...) {
                        failures[plan.port] = std::current_exception();
                        return;
                    }
                }
            });
        }
    } catch (...) {
        for (auto &worker : workers)
            if (worker.joinable())
                worker.join();
        throw;
    }
    for (auto &worker : workers)
        worker.join();
    for (const auto &failure : failures)
        if (failure)
            std::rethrow_exception(failure);
    const auto allocationMilliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - allocationStarted).count();
    LOG("All receive memory ready: ms=" + std::to_string(allocationMilliseconds), START);
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
