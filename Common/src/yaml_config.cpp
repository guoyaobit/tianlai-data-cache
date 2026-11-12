#include "yaml_config.h"
#include "protocol.h"
#include <yaml-cpp/yaml.h>
#include <arpa/inet.h>
#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>

static std::string field(const YAML::Node &tree, const std::string &key) {
    if (!tree[key] || !tree[key].IsScalar())
        throw std::runtime_error("missing YAML scalar: " + key);
    return tree[key].as<std::string>();
}
static void validateMapping(const YAML::Node &tree, const std::string &name) {
    if (!tree || !tree.IsMap())
        throw std::runtime_error("missing YAML mapping: " + name);
    std::set<std::string> keys;
    for (const auto &entry : tree) {
        if (!entry.first.IsScalar() || !keys.insert(entry.first.as<std::string>()).second)
            throw std::runtime_error("duplicate or invalid YAML key in " + name);
    }
}
static uint64_t number(const YAML::Node &tree, const std::string &key, uint64_t low,
                       uint64_t high) {
    uint64_t n = parseUnsigned(field(tree, key));
    if (n < low || n > high)
        throw std::runtime_error("invalid YAML field: " + key);
    return n;
}
static bool boolean(const YAML::Node &tree, const std::string &key, bool fallback) {
    if (!tree[key])
        return fallback;
    const auto value = field(tree, key);
    if (value == "true")
        return true;
    if (value == "false")
        return false;
    throw std::runtime_error("invalid YAML boolean: " + key);
}
static std::string ip(const YAML::Node &tree, const std::string &key) {
    auto value = field(tree, key);
    in_addr a{};
    if (inet_pton(AF_INET, value.c_str(), &a) != 1)
        throw std::runtime_error("invalid IPv4: " + key);
    return value;
}
// Read node and receive configuration, validate parameter ranges, and build the port mapping for the node type.
YamlConfig::YamlConfig(const std::string &recvFile, const std::string &serverFile,
                       StoreMode profile)
    : mode(profile) {
    const bool f0to2 = mode == StoreMode::F0_2;
    const bool remote = mode == StoreMode::F11_13;
    const auto cfg = YAML::LoadFile(recvFile);
    const auto node = YAML::LoadFile(serverFile);
    validateMapping(cfg, "receive config");
    validateMapping(node, "server config");
    serverId = number(node, "SERVERID", f0to2 ? 0 : remote ? 11 : 3, f0to2 ? 2 : remote ? 13 : 10);
    inputDataBytes = f0to2 ? 2304 : remote ? 8256 : 8832;
    integrationPerTrace = number(cfg, "integrationPerTrace", 1, 0x800000);
    periodMicroseconds = number(cfg, "periodMicroseconds", 1, 1000000);
    halfBufferSeconds = number(cfg, "halfBufferSeconds", 1, 50);
    maxEventSeconds = number(cfg, "maxEventSeconds", 1, 50);
    if (maxEventSeconds > halfBufferSeconds || 1000000 % periodMicroseconds)
        throw std::runtime_error("event must fit one half; period must divide one second");
    transitionBytes = number(cfg, "transitionMiB", 1, 65536) << 20;
    reserveMemoryBytes = number(cfg, "reserveMemoryMiB", 0, 1048576) << 20;
    reorderPeriods = number(cfg, "reorderPeriods", 1, 10000);
    settleMilliseconds = number(cfg, "settleMilliseconds", 1, 10000);
    transitionWarnPercent = number(cfg, "transitionWarnPercent", 1, 99);
    chunkPeriods = number(cfg, "chunkPeriods", 1, 64);
    socketTimeoutSeconds = number(cfg, "socketTimeoutSeconds", 1, 300);
    debug = boolean(cfg, "debug", false);
    if (cfg["acquisitionId"])
        throw std::runtime_error("acquisitionId is fixed internally; remove it from YAML");
    if (cfg["sendRateBitsPerSecond"])
        throw std::runtime_error("sendRateBitsPerSecond is obsolete; storage transfer is unlimited");
    if (cfg["udpFragmentBytes"])
        throw std::runtime_error("udpFragmentBytes is obsolete; remove it for ZeroMQ storage transport");
    prefaultMemory = number(cfg, "prefaultMemory", 0, 1) != 0;
    controlBindIp = ip(node, "controlBindIp");
    controlPeerIp = ip(node, "controlPeerIp");
    if (controlPeerIp == "0.0.0.0")
        throw std::runtime_error("controlPeerIp must name the controller");
    controlPort = number(node, "controlPort", 1024, 65535);
    logFilePath = field(node, "logFilePath");
    for (unsigned p = 0; p < (f0to2 ? 8U : 4U); ++p) {
        ReceivePortConfig port;
        port.deviceName = field(node, "deviceName" + std::to_string(p));
        const auto cpuKey = "cpuId" + std::to_string(p);
        port.cpuId = node[cpuKey] ? node[cpuKey].as<int>() : -1;
        if (port.cpuId < -1 || port.cpuId >= 1024)
            throw std::runtime_error("invalid cpuId");
        const auto simulationKey = "simulationPort" + std::to_string(p);
        port.simulationPort =
            node[simulationKey] ? number(node, simulationKey, 1024, 65535) : 0;
        port.packetId = f0to2    ? serverId * 8 + p
                        : remote ? 2 * ((serverId - 11) * 4 + p)
                                 : 24 + (serverId - 3) * 2 + p / 2;
        port.boardBegin = f0to2 ? 0 : remote ? 36 : (p % 2) * 18;
        port.boardNum = f0to2 ? 36 : remote ? 12 : 18;
        port.packetsPerBoard = remote ? 2 : 1;
        if (remote) {
            port.frequencyBegin = ((serverId - 11) * 4 + p) * 86;
            port.frequencyCount = 86;
            port.validFrequencyCount = std::min(86U, 1024U - port.frequencyBegin);
        } else {
            port.frequencyCount = f0to2 ? 12 : 46;
            port.frequencyBegin = f0to2 ? port.packetId * 12 : 288 + (port.packetId - 24) * 46;
            port.validFrequencyCount = port.frequencyCount;
        }
        receivePorts.push_back(port);
    }
    sendLink.ethName = field(node, "sendEthName");
    sendLink.sourceIp = ip(node, "sendSourceIp");
    sendLink.destinationIp = ip(node, "sendDestinationIp");
    for (unsigned lane = 0; lane < 2; ++lane)
        sendLink.destinationPorts[lane] =
            number(node, "sendDestinationPort" + std::to_string(lane), 1024, 65535);
    if (sendLink.destinationPorts[0] == sendLink.destinationPorts[1])
        throw std::runtime_error("storage lane destination ports must be distinct");
    if (reorderPeriods >= halfPeriods() || transitionPeriods() < reorderPeriods * 2ULL)
        throw std::runtime_error("transition buffer/half buffer too small for reorder window");
}
uint64_t YamlConfig::transitionPeriods() const {
    uint64_t stride = receivePorts.front().packetSlots() * uint64_t(inputDataBytes + 16);
    return transitionBytes / receivePorts.size() / stride;
}
uint64_t YamlConfig::allocationBytes() const {
    uint64_t stride = receivePorts.front().packetSlots() * uint64_t(inputDataBytes + 16);
    return receivePorts.size() * (2 * halfPeriods() + transitionPeriods()) * (stride + 32);
}
