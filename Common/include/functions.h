#ifndef STORE_FUNCTION_H_
#define STORE_FUNCTION_H_
#include "receive_pool.h"
#include "yaml_config.h"
#include <atomic>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
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

enum class EventState {
    Idle,
    Preparing,
    Prepared,
    Queued,
    Sending,
    SendFailed,
    Stored,
    Released,
    Rejected,
    Cancelled
};

inline const char *eventStateName(EventState state) {
    switch (state) {
    case EventState::Idle: return "IDLE";
    case EventState::Preparing: return "PREPARING";
    case EventState::Prepared: return "PREPARED";
    case EventState::Queued: return "QUEUED";
    case EventState::Sending: return "SENDING";
    case EventState::SendFailed: return "SEND_FAILED";
    case EventState::Stored: return "STORED";
    case EventState::Released: return "RELEASED";
    case EventState::Rejected: return "REJECTED";
    case EventState::Cancelled: return "CANCELLED";
    }
    return "UNKNOWN";
}
inline bool isActiveEventState(EventState state) {
    return state == EventState::Preparing || state == EventState::Prepared ||
           state == EventState::Queued || state == EventState::Sending ||
           state == EventState::SendFailed || state == EventState::Stored;
}

struct EventInfo {
    std::string id, error;
    EventState state = EventState::Idle;
    uint64_t start = 0, count = 0, missing = 0, sentBytes = 0;
    uint64_t freezeMilliseconds = 0, sendMilliseconds = 0;
    uint64_t transitionPeakPeriods = 0, transitionCapacityPeriods = 0;
    uint64_t sendRateBitsPerSecond = 0;
    bool committed = false, cancelRequested = false;
};
struct CacheRuntime {
    YamlConfig *config = nullptr;
    ThreadFlags *flags = nullptr;
    bool simulation = false;
    std::vector<std::unique_ptr<ReceiveRingBuffer>> receiveBuffers;
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
void InitRecvAntennaThread(CacheRuntime *para, unsigned port);
void InitControlThread(CacheRuntime *para);
void InitSendStoreThread(CacheRuntime *para);
std::string ProcessControl(CacheRuntime &para, const std::string &request);
int StoreMain(int argc, char **argv, StoreMode mode);
#endif
