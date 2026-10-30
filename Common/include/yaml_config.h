#ifndef STORE_YAMLCONFIG_H_
#define STORE_YAMLCONFIG_H_
#include <cstdint>
#include <string>
#include <vector>

enum class StoreMode { F0_2, F3_10, F11_13 };

struct ReceivePortConfig {
    std::string deviceName;
    // packetId is the first accepted ID; remote boards send two IDs per integration.
    unsigned packetId = 0, boardBegin = 0, boardNum = 0;
    unsigned packetsPerBoard = 1;
    unsigned frequencyBegin = 0, frequencyCount = 0, validFrequencyCount = 0;
    unsigned packetSlots() const {
        return boardNum * packetsPerBoard;
    }
    int cpuId = -1;
    unsigned simulationPort = 0;
};
struct SendPortConfig {
    std::string ethName, sourceIp, destinationIp;
    unsigned destinationPort = 0;
};
class YamlConfig {
  public:
    YamlConfig(const std::string &recvFile, const std::string &serverFile, StoreMode mode);
    unsigned getServerID() const {
        return serverId;
    }
    unsigned getIntegrationPerTrace() const {
        return integrationPerTrace;
    }
    const std::string &getDeviceName(unsigned port) const {
        return receivePorts.at(port).deviceName;
    }
    uint64_t halfPeriods() const {
        return halfBufferSeconds * 1000000ULL / periodMicroseconds;
    }
    uint64_t maxEventPeriods() const {
        return maxEventSeconds * 1000000ULL / periodMicroseconds;
    }
    uint64_t transitionPeriods() const;
    uint64_t allocationBytes() const;
    StoreMode mode;
    unsigned serverId = 0, inputDataBytes = 0, integrationPerTrace = 50000;
    unsigned periodMicroseconds = 200, halfBufferSeconds = 50, maxEventSeconds = 5;
    unsigned reorderPeriods = 50, settleMilliseconds = 20, chunkPeriods = 32;
    unsigned controlPort = 19000, socketTimeoutSeconds = 15;
    // ZeroMQ application-byte rate limit; excludes network overhead and retransmissions.
    uint64_t sendRateBitsPerSecond = 32000000000ULL / 14;
    uint64_t transitionBytes = 32ULL << 30, reserveMemoryBytes = 48ULL << 30;
    bool prefaultMemory = true;
    std::string acquisitionId, controlBindIp, controlPeerIp, logFilePath;
    std::vector<ReceivePortConfig> receivePorts;
    SendPortConfig sendPorts[1];
};
#endif
