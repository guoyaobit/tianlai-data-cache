#include "functions.h"
#include "log_output.h"
#include <chrono>
#include <string>

namespace {
uint64_t monotonicMilliseconds() {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                     std::chrono::steady_clock::now().time_since_epoch())
                                     .count());
}

void logTransition(RuntimeState state) {
    LOG("Runtime state: " + std::string(runtimeStateName(state)),
        state == RuntimeState::Error ? ERRO : INFO);
}
} // namespace

const char *runtimeStateName(RuntimeState state) {
    switch (state) {
    case RuntimeState::Starting: return "STARTING";
    case RuntimeState::PreparingMemory: return "PREPARING_MEMORY";
    case RuntimeState::WaitingForData: return "WAITING_FOR_DATA";
    case RuntimeState::CachingData: return "CACHING_DATA";
    case RuntimeState::PreparingEvent: return "PREPARING_EVENT";
    case RuntimeState::EventReady: return "EVENT_READY";
    case RuntimeState::SendingCachedData: return "SENDING_CACHED_DATA";
    case RuntimeState::StorageConfirmed: return "STORAGE_CONFIRMED";
    case RuntimeState::SendFailed: return "SEND_FAILED";
    case RuntimeState::Error: return "ERROR";
    case RuntimeState::Stopping: return "STOPPING";
    }
    return "UNKNOWN";
}

void setRuntimeState(CacheRuntime &runtime, RuntimeState state) {
    const auto previous = runtime.runtimeState.exchange(state);
    if (previous == state)
        return;
    runtime.runtimeStateChangedMs = monotonicMilliseconds();
    logTransition(state);
}

void setPassiveRuntimeState(CacheRuntime &runtime) {
    setRuntimeState(runtime, runtime.hasAcceptedData.load() ? RuntimeState::CachingData
                                                            : RuntimeState::WaitingForData);
}

void noteAcceptedPacket(CacheRuntime &runtime) {
    bool noData = false;
    if (!runtime.hasAcceptedData.compare_exchange_strong(noData, true,
                                                         std::memory_order_relaxed))
        return;
    RuntimeState expected = RuntimeState::WaitingForData;
    if (runtime.runtimeState.compare_exchange_strong(expected, RuntimeState::CachingData)) {
        runtime.runtimeStateChangedMs = monotonicMilliseconds();
        logTransition(RuntimeState::CachingData);
    }
}

uint64_t runtimeStateAgeMilliseconds(const CacheRuntime &runtime) {
    const auto changed = runtime.runtimeStateChangedMs.load();
    const auto now = monotonicMilliseconds();
    return changed && now >= changed ? now - changed : 0;
}
