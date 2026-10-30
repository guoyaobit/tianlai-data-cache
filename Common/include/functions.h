#ifndef STORE_FUNCTION_H_
#define STORE_FUNCTION_H_
#include "receive_pool.h"
#include "yaml_config.h"
#include <atomic>
#include <deque>
#include <map>
#include <memory>
#include <thread>
#include <vector>

// Shared stop flags for the receive workers and control loop.
struct ThreadFlags {
    std::atomic<bool> ifRecvAntenna{true};
    std::atomic<bool> ifControl{true};

    void threadStop() {
        ifRecvAntenna = false;
        ifControl = false;
    }
};

struct EventInfo {
    std::string id, state = "IDLE", error;
    uint64_t start = 0, count = 0, missing = 0, sentBytes = 0;
    bool committed = false, cancelRequested = false;
};
struct PARAMETERS {
    YamlConfig *config = nullptr;
    ThreadFlags *flags = nullptr;
    bool simulation = false;
    std::vector<std::unique_ptr<EthFrmRcvPool>> pEthRcvPool;
    std::vector<std::thread> receiveThreads;
    std::thread eventThread;
    std::mutex eventMutex;
    EventInfo event;
    std::map<std::string, EventInfo> history;
    std::deque<std::string> historyOrder;
    std::atomic<unsigned> initializedPorts{0};
    std::atomic<uint64_t> ignoredPackets{0};
    std::atomic<bool> failed{false};
};
void InitRecvAntennaThread(PARAMETERS *para, unsigned port);
void InitControlThread(PARAMETERS *para);
void InitSendStoreThread(PARAMETERS *para);
std::string ProcessControl(PARAMETERS &para, const std::string &request);
int StoreMain(int argc, char **argv, StoreMode mode);
#endif
