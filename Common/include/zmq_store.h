#ifndef STORE_ZMQSTORE_H_
#define STORE_ZMQSTORE_H_
#include "functions.h"

// One pacer per send lane and event. No accumulated idle credits.
class StorePacer {
  public:
    explicit StorePacer(uint64_t bitsPerSecond) : rate(bitsPerSecond) {}
    void wait(size_t bytes, const ThreadFlags *flags = nullptr);
    uint64_t bitsPerSecond() const { return rate; }

  private:
    uint64_t rate;
    std::mutex mutex;
    std::chrono::steady_clock::time_point next{};
};

// One ZeroMQ REQ socket per lane. Each TLSTORE1 message has one reply.
// A metadata retry may receive the final durable result immediately.
class StoreConnection {
  public:
    StoreConnection(CacheRuntime &para, unsigned lane, StorePacer &pacer);
    ~StoreConnection();
    StoreConnection(const StoreConnection &) = delete;
    StoreConnection &operator=(const StoreConnection &) = delete;
    void sendMessage(const std::vector<unsigned char> &message, bool final = false,
                     bool allowStored = false);
    bool hasResult() const { return !result.empty(); }
    std::string resultJson() const;

  private:
    CacheRuntime &para;
    StorePacer &pacer;
    unsigned laneIndex = 0;
    void *context = nullptr;
    void *socket = nullptr;
    std::vector<unsigned char> result;
};
#endif
