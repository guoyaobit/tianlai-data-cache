#ifndef STORE_PROTOCOL_H_
#define STORE_PROTOCOL_H_
#include <boost/property_tree/ptree.hpp>
#include <cstdint>
#include <string>
#include <vector>

constexpr unsigned INPUT_HEADER_BYTES = 16;
constexpr size_t MAX_CONTROL_BYTES = 1200;
constexpr uint64_t INVALID_PERIOD = UINT64_MAX;
uint64_t parseUnsigned(const std::string &s);
bool validId(const std::string &s);
std::string quoteJson(const std::string &s);
boost::property_tree::ptree parseControl(const std::string &s);
bool hasField(const boost::property_tree::ptree &tree, const std::string &key);
std::string getId(const boost::property_tree::ptree &tree, const std::string &key);
uint64_t getNumber(const boost::property_tree::ptree &tree, const std::string &key);
void put32(std::vector<unsigned char> &v, uint32_t n);
void put64(std::vector<unsigned char> &v, uint64_t n);
struct InputHeader {
    uint32_t trace = 0, integration = 0;
    unsigned ifTrace = 0, board = 0, packetId = 0;
    uint64_t absolute(unsigned perTrace) const {
        return uint64_t(trace) * perTrace + integration;
    }
};
InputHeader decodeInput(const unsigned char *data);
// Parses Ethernet + optional VLAN(s) + unfragmented IPv4/UDP, without a fixed 42-byte offset.
bool inputPayload(const unsigned char *frame, size_t size, const unsigned char *&data,
                  size_t &length);
#endif
