#ifndef STORE_ZMQSTORE_H_
#define STORE_ZMQSTORE_H_
#include "functions.h"

// One ZeroMQ REQ socket per lane. Each TLSTORE1 message has one reply.
// A metadata retry may receive the final durable result immediately.
class StoreConnection {
  public:
    StoreConnection(CacheRuntime &para, unsigned lane);
    ~StoreConnection();
    StoreConnection(const StoreConnection &) = delete;
    StoreConnection &operator=(const StoreConnection &) = delete;
    void sendMessage(const std::vector<unsigned char> &message, bool final = false,
                     bool allowStored = false);
    bool hasResult() const { return !result.empty(); }
    std::string resultJson() const;

  private:
    CacheRuntime &para;
    unsigned laneIndex = 0;
    void *context = nullptr;
    void *socket = nullptr;
    bool connectionReadyLogged = false;
    std::string remoteEndpoint;
    std::vector<unsigned char> result;
};
#endif
