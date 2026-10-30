#include "protocol.h"
#include <boost/property_tree/json_parser.hpp>
#include <cctype>
#include <iomanip>
#include <set>
#include <sstream>
#include <stdexcept>

uint64_t parseUnsigned(const std::string &s) {
    if (s.empty())
        throw std::runtime_error("empty unsigned number");
    uint64_t n = 0;
    for (char c : s) {
        if (c < '0' || c > '9' || n > (UINT64_MAX - unsigned(c - '0')) / 10)
            throw std::runtime_error("invalid unsigned number");
        n = n * 10 + unsigned(c - '0');
    }
    return n;
}
bool validId(const std::string &s) {
    if (s.empty() || s.size() > 64)
        return false;
    for (unsigned char c : s)
        if (!std::isalnum(c) && c != '-' && c != '_')
            return false;
    return true;
}
std::string quoteJson(const std::string &s) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : s) {
        if (c == '"' || c == '\\')
            out << '\\' << c;
        else if (c < 32)
            out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
        else
            out << c;
    }
    out << '"';
    return out.str();
}
static void uniqueKeys(const boost::property_tree::ptree &t) {
    std::set<std::string> keys;
    for (const auto &kv : t) {
        if (kv.first.empty() || !keys.insert(kv.first).second)
            throw std::runtime_error("duplicate/array JSON key");
        uniqueKeys(kv.second);
    }
}
boost::property_tree::ptree parseControl(const std::string &s) {
    if (s.empty() || s.size() > MAX_CONTROL_BYTES)
        throw std::runtime_error("invalid control length");
    auto pos = s.find_first_not_of(" \r\n\t");
    if (pos == std::string::npos || s[pos] != '{')
        throw std::runtime_error("JSON object required");
    std::istringstream in(s);
    boost::property_tree::ptree tree;
    boost::property_tree::read_json(in, tree);
    uniqueKeys(tree);
    if (getNumber(tree, "version") != 1)
        throw std::runtime_error("unsupported protocol version");
    return tree;
}
std::string getId(const boost::property_tree::ptree &tree, const std::string &key) {
    auto id = tree.get<std::string>(key);
    if (!validId(id) || !tree.get_child(key).empty())
        throw std::runtime_error("invalid ID: " + key);
    return id;
}
uint64_t getNumber(const boost::property_tree::ptree &tree, const std::string &key) {
    if (!tree.get_child(key).empty())
        throw std::runtime_error("scalar required: " + key);
    return parseUnsigned(tree.get<std::string>(key));
}
void put32(std::vector<unsigned char> &v, uint32_t n) {
    for (int i = 3; i >= 0; --i)
        v.push_back((n >> (8 * i)) & 255);
}
void put64(std::vector<unsigned char> &v, uint64_t n) {
    for (int i = 7; i >= 0; --i)
        v.push_back((n >> (8 * i)) & 255);
}
// Parse the marker, integration number, board number, and packet number from the FPGA payload header.
InputHeader decodeInput(const unsigned char *d) {
    InputHeader h;
    h.valid = d[0] >> 4;
    h.trace = (uint32_t(d[0] & 15) << 16) | (uint32_t(d[1]) << 8) | d[2];
    h.ifTrace = d[3] >> 7;
    h.integration = (uint32_t(d[3] & 127) << 16) | (uint32_t(d[4]) << 8) | d[5];
    h.board = d[6];
    h.packetId = d[7];
    return h;
}
static unsigned be16(const unsigned char *p) {
    return (unsigned(p[0]) << 8) | p[1];
}
// Validate the Ethernet, optional VLAN, IPv4, and UDP encapsulation, then return the raw payload.
bool inputPayload(const unsigned char *f, size_t n, const unsigned char *&d, size_t &len) {
    if (n < 14)
        return false;
    size_t offset = 14;
    unsigned type = be16(f + 12);
    for (int i = 0; i < 2 && (type == 0x8100 || type == 0x88a8); ++i) {
        if (n < offset + 4)
            return false;
        type = be16(f + offset + 2);
        offset += 4;
    }
    if (type != 0x0800 || n < offset + 20 || (f[offset] >> 4) != 4)
        return false;
    unsigned ihl = (f[offset] & 15) * 4, total = be16(f + offset + 2);
    if (ihl < 20 || total < ihl + 8 || n < offset + total || f[offset + 9] != 17 ||
        (be16(f + offset + 6) & 0x3fff))
        return false;
    const auto *udp = f + offset + ihl;
    unsigned ulen = be16(udp + 4);
    if (ulen < 8 || ulen != total - ihl)
        return false;
    d = udp + 8;
    len = ulen - 8;
    return true;
}
