// TLSTORE1 ZeroMQ disk receiver. Each server/lane has one ROUTER thread that
// validates and writes its sequential request stream without an extra disk thread.
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#endif
#include <zmq.h>
#include <nlohmann/json.hpp>
#ifndef STORAGE_RECEIVER_NO_MAIN
#include <yaml-cpp/yaml.h>
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <climits>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <exception>
#include <initializer_list>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#ifndef _WIN32
#include <arpa/inet.h>
#include <sys/socket.h>
#endif

namespace fs = std::filesystem;
using json = nlohmann::json;
using Bytes = std::vector<unsigned char>;
constexpr size_t MAX_MESSAGE = 13 * 1024 * 1024;
constexpr size_t MAX_SESSIONS_PER_PORT = 1;
constexpr uint64_t DATA_SYNC_BYTES = 256ULL * 1024 * 1024;
constexpr auto DATA_SYNC_INTERVAL = std::chrono::seconds(1);
constexpr unsigned CACHE_NODE_COUNT = 14;
constexpr unsigned LANE_COUNT = 2;
constexpr unsigned STORAGE_PORT_COUNT = CACHE_NODE_COUNT * LANE_COUNT;
constexpr size_t CHUNK_HEADER = 40, FRAME_HEADER = 24;
static volatile std::sig_atomic_t stopping = 0;
static void stop_handler(int) { stopping = 1; }

static std::runtime_error system_error(const std::string &operation) {
    return std::runtime_error(operation + ": " + std::strerror(errno));
}
static void require(bool condition, const char *message) {
    if (!condition) throw std::runtime_error(message);
}
static uint32_t read32(const unsigned char *p) {
    return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
}
static uint64_t read64(const unsigned char *p) {
    return uint64_t(read32(p)) << 32 | read32(p + 4);
}
static Bytes framed_json(const json &value) {
    const auto body = value.dump();
    require(!body.empty() && body.size() <= 1200, "result JSON too long");
    const auto n = static_cast<uint32_t>(body.size());
    Bytes result{static_cast<unsigned char>(n >> 24), static_cast<unsigned char>(n >> 16),
                 static_cast<unsigned char>(n >> 8), static_cast<unsigned char>(n)};
    result.insert(result.end(), body.begin(), body.end());
    return result;
}
static bool valid_id(const std::string &s) {
    if (s.empty() || s.size() > 64) return false;
    return std::all_of(s.begin(), s.end(), [](unsigned char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
               (c >= '0' && c <= '9') || c == '_' || c == '-';
    });
}
static uint64_t integer(const json &j, const char *name) {
    const auto &value = j.at(name);
    require(value.is_number_unsigned() || value.is_number_integer(), "integer metadata required");
    if (value.is_number_unsigned()) return value.get<uint64_t>();
    const auto n = value.get<int64_t>();
    require(n >= 0, "negative metadata value");
    return static_cast<uint64_t>(n);
}
static void sync_dir(const fs::path &path) {
    int fd = ::open(path.string().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) throw system_error("open directory");
    int rc = ::fsync(fd), error = errno;
    ::close(fd);
    if (rc) { errno = error; throw system_error("fsync directory"); }
}
static void ensure_dir(const fs::path &parent, const std::string &name) {
    const auto child = parent / name;
    std::error_code error;
    fs::create_directory(child, error);
    if (error) throw std::runtime_error("create directory: " + error.message());
    require(fs::is_directory(child), "storage path is not a directory");
    sync_dir(parent);
}
static void write_all(int fd, const unsigned char *data, size_t size, uint64_t offset) {
    while (size) {
        ssize_t written = ::pwrite(fd, data, size, static_cast<off_t>(offset));
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) throw system_error("write file");
        data += written; size -= static_cast<size_t>(written); offset += written;
    }
}
static void atomic_json(const fs::path &path, const json &value) {
    const auto temp = path.string() + ".tmp";
    int fd = ::open(temp.c_str(), O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0640);
    if (fd < 0) throw system_error("open JSON temporary file");
    try {
        const auto text = value.dump(2);
        write_all(fd, reinterpret_cast<const unsigned char *>(text.data()), text.size(), 0);
        if (::fsync(fd)) throw system_error("fsync JSON");
        if (::close(fd)) { fd = -1; throw system_error("close JSON"); }
        fd = -1;
        if (::rename(temp.c_str(), path.string().c_str())) throw system_error("rename JSON");
        sync_dir(path.parent_path());
    } catch (...) { if (fd >= 0) ::close(fd); throw; }
}

struct Shared {
    std::mutex mutex;
    std::set<unsigned> active;
};

// Owns one server/lane transfer; both lanes may write disjoint port files concurrently.
class EventWriter {
    fs::path root, directory;
    Shared &shared;
    unsigned expected_server = UINT_MAX, expected_lane = UINT_MAX;
    json metadata;
    std::string event, acquisition;
    unsigned server = 0, lane = 0, ports = 0, boards = 0;
    unsigned packet_bytes = 0, packets_per_board = 1;
    uint64_t start = 0, count = 0, per_trace = 0, stride = 0;
    bool claimed = false;
    std::vector<int> files;
    std::vector<uint64_t> progress, missing;
    std::vector<bool> selected;
    uint64_t unsynced_bytes = 0;
    std::chrono::steady_clock::time_point last_data_sync;

    void sync_data(bool force) {
        const auto now = std::chrono::steady_clock::now();
        if (!force && unsynced_bytes < DATA_SYNC_BYTES && now - last_data_sync < DATA_SYNC_INTERVAL)
            return;
        for (int fd : files)
            if (fd >= 0 && ::fdatasync(fd))
                throw system_error("fdatasync event file");
        unsynced_bytes = 0;
        last_data_sync = now;
    }

  public:
    EventWriter(fs::path base, Shared &state, unsigned server_id = UINT_MAX,
                unsigned lane_id = UINT_MAX)
        : root(std::move(base)), shared(state), expected_server(server_id), expected_lane(lane_id) {}
    ~EventWriter() {
        for (int fd : files) if (fd >= 0) ::close(fd);
        if (claimed) {
            std::lock_guard<std::mutex> lock(shared.mutex);
            shared.active.erase(server * LANE_COUNT + lane);
        }
    }
    // Returns an already durable STORED result when the same transfer is retried.
    // This avoids rewriting an event merely because the previous final reply was lost.
    Bytes begin(const Bytes &message) {
        require(message.size() >= 4 && message.size() <= 1204, "invalid metadata length");
        auto n = read32(message.data());
        require(n > 0 && n == message.size() - 4, "invalid metadata length");
        metadata = json::parse(message.begin() + 4, message.end());
        require(metadata.is_object(), "metadata must be an object");
        event = metadata.at("event_id").get<std::string>();
        acquisition = metadata.at("acquisition_id").get<std::string>();
        require(valid_id(event) && valid_id(acquisition), "invalid event/acquisition identifier");
        auto node = integer(metadata, "server_id"), lane_value = integer(metadata, "lane");
        auto channels = integer(metadata, "channel_count");
        start = integer(metadata, "start_period"); count = integer(metadata, "period_count");
        per_trace = integer(metadata, "integration_per_trace"); auto period_us = integer(metadata, "period_microseconds");
        require(integer(metadata, "version") == 1 && metadata.at("format") == "TLSTORE1" &&
                (channels == 288 || channels == 384) &&
                node < (channels == 288 ? 11U : CACHE_NODE_COUNT) && lane_value < LANE_COUNT &&
                period_us >= 1 && period_us <= 1000000 &&
                1000000 % period_us == 0 && per_trace >= 1 && per_trace <= 0x800000 &&
                start < (1ULL << 20) * per_trace && count >= 1 && count <= 5000000 / period_us &&
                start + count <= (1ULL << 20) * per_trace, "invalid stream metadata");
        server = static_cast<unsigned>(node);
        lane = static_cast<unsigned>(lane_value);
        require((expected_server == UINT_MAX || server == expected_server) &&
                    (expected_lane == UINT_MAX || lane == expected_lane),
                "server/lane connected to the wrong storage port");
        if (server < 3) { ports = 8; boards = 36; packet_bytes = 2320; }
        else if (server < 11) { ports = 4; boards = 18; packet_bytes = 8848; }
        else {
            ports = 4; boards = 24; packet_bytes = 8272; packets_per_board = 2;
            require(metadata.at("profile") == "remote96" && integer(metadata, "packets_per_board") == 2 &&
                    integer(metadata, "physical_board_count") == 12 && integer(metadata, "frequency_bins_per_port") == 86 &&
                    integer(metadata, "total_valid_frequencies") == 1024 && integer(metadata, "fft_per_packet") == 12,
                    "remote packet-pair metadata required");
        }
        require(integer(metadata, "port_count") == ports && integer(metadata, "packet_bytes") == packet_bytes,
                "input profile mismatch");
        const auto &port_list = metadata.at("ports");
        require(port_list.is_array() && port_list.size() == ports / LANE_COUNT,
                "invalid lane port list");
        selected.assign(ports, false);
        for (size_t index = 0; index < port_list.size(); ++index) {
            const auto port = port_list.at(index).get<unsigned>();
            require(port < ports && port % LANE_COUNT == lane && !selected[port],
                    "invalid lane port assignment");
            selected[port] = true;
        }
        ensure_dir(root, acquisition); ensure_dir(root / acquisition, event);
        ensure_dir(root / acquisition / event, "server-" + std::to_string(server));
        directory = root / acquisition / event / ("server-" + std::to_string(server));
        const auto lane_name = "lane-" + std::to_string(lane);
        auto meta_path = directory / (lane_name + ".meta.json");
        if (fs::exists(meta_path)) {
            std::ifstream input(meta_path); json old; input >> old;
            require(old == metadata, "event metadata conflicts with an existing transfer");
        }
        stride = FRAME_HEADER + uint64_t(boards) * packet_bytes;
        auto marker = directory / (lane_name + ".stored.json");
        if (fs::exists(marker)) {
            std::ifstream input(marker); json stored; input >> stored;
            require(stored.at("status") == "STORED" && stored.at("event_id") == event &&
                    stored.at("acquisition_id") == acquisition && integer(stored, "server_id") == server &&
                    integer(stored, "lane") == lane, "invalid existing completion marker");
            for (unsigned p = 0; p < ports; ++p) {
                if (!selected[p]) continue;
                const auto path = directory / ("port-" + std::to_string(p) + ".bin");
                require(fs::is_regular_file(path) && fs::file_size(path) == count * stride,
                        "stored event file is missing or has the wrong size");
            }
            json ack = {{"version", 1}, {"status", "STORED"}, {"event_id", event},
                        {"acquisition_id", acquisition}, {"server_id", server}, {"lane", lane}};
            return framed_json(ack);
        }
        {
            std::lock_guard<std::mutex> lock(shared.mutex);
            require(shared.active.insert(server * LANE_COUNT + lane).second,
                    "server lane already receiving");
            claimed = true;
        }
        atomic_json(meta_path, metadata);
        progress.assign(ports, start); missing.assign(ports, 0);
        files.assign(ports, -1);
        for (unsigned p = 0; p < ports; ++p) {
            if (!selected[p]) continue;
            auto path = directory / ("port-" + std::to_string(p) + ".bin");
            files[p] = ::open(path.string().c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0640);
            if (files[p] < 0) throw system_error("open event file");
            if (::ftruncate(files[p], static_cast<off_t>(count * stride))) throw system_error("resize event file");
        }
        last_data_sync = std::chrono::steady_clock::now();
        return {};
    }
    // A block acknowledgment means validated and written. Only STORED is durable.
    void block(const Bytes &message) {
        require(message.size() >= CHUNK_HEADER, "truncated chunk header");
        const auto *p = message.data();
        auto kind = read32(p), port = read32(p + 4), packet = read32(p + 8), board = read32(p + 12);
        auto first = read64(p + 16), n = read64(p + 24);
        auto size = read32(p + 32), reserved = read32(p + 36);
        require(kind == 1 && port < ports && selected[port], "invalid chunk type/port");
        unsigned expected_packet = server < 3 ? server * 8 + port : server >= 11 ?
            2 * ((server - 11) * 4 + port) : 24 + (server - 3) * 2 + port / 2;
        unsigned expected_board = server < 3 ? 0 : server >= 11 ? 36 : (port % 2) * 18;
        require(packet == expected_packet && board == expected_board && first == progress[port] &&
                n >= 1 && n <= 64 && first + n <= start + count && size == n * stride && reserved == 0 &&
                message.size() == CHUNK_HEADER + size, "invalid chunk range/layout");
        const auto *body = p + CHUNK_HEADER;
        uint64_t absent = 0;
        for (uint64_t i = 0; i < n; ++i) {
            const auto *frame = body + i * stride;
            auto period = read64(frame), mask = read64(frame + 8);
            require(period == first + i && read32(frame + 16) == packet_bytes &&
                    read32(frame + 20) == boards && (mask >> boards) == 0, "invalid frame metadata");
            absent += boards - static_cast<unsigned>(__builtin_popcountll(mask));
            for (unsigned b = 0; b < boards; ++b) {
                const auto *raw = frame + FRAME_HEADER + uint64_t(b) * packet_bytes;
                if (mask & (1ULL << b)) {
                    auto trace = uint32_t(raw[0] & 15) << 16 | uint32_t(raw[1]) << 8 | raw[2];
                    auto integration = uint32_t(raw[3] & 127) << 16 | uint32_t(raw[4]) << 8 | raw[5];
                    require(raw[0] >> 4 == 15 && integration < per_trace && trace * per_trace + integration == period &&
                            raw[6] == b / packets_per_board + board && raw[7] == packet + b % packets_per_board,
                            "raw input header mismatch");
                } else {
                    require(std::all_of(raw, raw + packet_bytes, [](unsigned char c) { return c == 0; }),
                            "missing packet slot was not zero-filled");
                }
            }
        }
        write_all(files[port], body, size, (first - start) * stride);
        unsynced_bytes += size;
        sync_data(false);
        progress[port] += n; missing[port] += absent;
    }
    Bytes finish(const Bytes &message) {
        require(message.size() == CHUNK_HEADER && read32(message.data()) == 2 &&
                std::all_of(message.begin() + 4, message.end(), [](unsigned char c) { return c == 0; }), "invalid END record");
        for (unsigned p = 0; p < ports; ++p) {
            if (!selected[p]) continue;
            require(progress[p] == start + count, "incomplete lane transfer");
        }
        sync_data(true);
        json ack = {{"version", 1}, {"status", "STORED"}, {"event_id", event},
                    {"acquisition_id", acquisition}, {"server_id", server}, {"lane", lane}};
        json marker = ack; marker["missing_packets"] = json::object();
        for (unsigned p = 0; p < ports; ++p) {
            if (!selected[p]) continue;
            marker["missing_packets"][std::to_string(p)] = missing[p];
        }
        atomic_json(directory / ("lane-" + std::to_string(lane) + ".stored.json"), marker);
        return framed_json(ack);
    }
};

struct ZmqMessage {
    zmq_msg_t value;
    ZmqMessage() { if (zmq_msg_init(&value)) throw std::runtime_error("zmq_msg_init failed"); }
    ~ZmqMessage() { zmq_msg_close(&value); }
    ZmqMessage(const ZmqMessage &) = delete;
    ZmqMessage &operator=(const ZmqMessage &) = delete;
};
static void option(void *socket, int key, int value) {
    if (zmq_setsockopt(socket, key, &value, sizeof(value)))
        throw std::runtime_error(std::string("ZeroMQ option: ") + zmq_strerror(zmq_errno()));
}
static void reply(void *router, const std::string &identity, const Bytes &body) {
    if (zmq_send(router, identity.data(), identity.size(), ZMQ_SNDMORE | ZMQ_DONTWAIT) < 0 ||
        zmq_send(router, nullptr, 0, ZMQ_SNDMORE | ZMQ_DONTWAIT) < 0 ||
        zmq_send(router, body.data(), body.size(), ZMQ_DONTWAIT) < 0)
        std::cerr << "receiver reply failed: " << zmq_strerror(zmq_errno()) << '\n';
}
static Bytes error_reply(const std::string &reason) {
    auto message = "ERROR:" + reason;
    return Bytes(message.begin(), message.end());
}

static json read_json_file(const fs::path &path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("open JSON: " + path.string());
    json value;
    input >> value;
    require(value.is_object(), "JSON object required");
    return value;
}

// Builds one result after both lanes from all nodes selected by channel_count are durable.
// The result itself is persisted before it can be sent to the backend.
static std::optional<json> build_completion_notice(const fs::path &event_directory) {
    const auto first_path = event_directory / "server-0" / "lane-0.meta.json";
    if (!fs::is_regular_file(first_path)) return std::nullopt;
    const json first = read_json_file(first_path);
    const auto channel_count = integer(first, "channel_count");
    require(channel_count == 288 || channel_count == 384, "invalid event channel count");
    const unsigned expected_nodes = channel_count == 288 ? 11U : CACHE_NODE_COUNT;
    json missing = json::object();
    for (unsigned server = 0; server < expected_nodes; ++server) {
        const auto directory = event_directory / ("server-" + std::to_string(server));
        uint64_t absent = 0;
        for (unsigned lane = 0; lane < LANE_COUNT; ++lane) {
            const auto prefix = "lane-" + std::to_string(lane);
            const auto meta_path = directory / (prefix + ".meta.json");
            const auto marker_path = directory / (prefix + ".stored.json");
            if (!fs::is_regular_file(meta_path) || !fs::is_regular_file(marker_path))
                return std::nullopt;
            const auto meta = read_json_file(meta_path);
            const auto marker = read_json_file(marker_path);
            require(integer(meta, "server_id") == server && integer(meta, "lane") == lane &&
                    meta.at("acquisition_id") == event_directory.parent_path().filename().string() &&
                    meta.at("event_id") == event_directory.filename().string() &&
                    marker.at("status") == "STORED" && integer(marker, "server_id") == server &&
                    integer(marker, "lane") == lane && marker.at("event_id") == meta.at("event_id") &&
                    marker.at("acquisition_id") == meta.at("acquisition_id"),
                    "node completion marker mismatch");
            if (server != 0 || lane != 0) {
                for (const char *field : {"format", "acquisition_id", "event_id", "start_period",
                                          "period_count", "integration_per_trace", "period_microseconds",
                                          "channel_count"})
                    require(meta.at(field) == first.at(field), "node event metadata mismatch");
            }
            const auto &by_port = marker.at("missing_packets");
            require(by_port.is_object(), "missing packet object required");
            for (auto item = by_port.begin(); item != by_port.end(); ++item) {
                require(item.value().is_number_unsigned() || item.value().is_number_integer(),
                        "invalid missing packet count");
                const auto value = item.value().get<int64_t>();
                require(value >= 0, "negative missing packet count");
                absent += static_cast<uint64_t>(value);
            }
        }
        missing[std::to_string(server)] = absent;
    }
    require(first.at("format") == "TLSTORE1", "unsupported event format");
    const auto start = integer(first, "start_period");
    const auto count = integer(first, "period_count");
    const auto per_trace = integer(first, "integration_per_trace");
    const auto end = start + count;
    json notice = {{"version", 1}, {"command", "CAPTURE_RESULT"}, {"status", "STORED"},
                   {"acquisition_id", first.at("acquisition_id")}, {"event_id", first.at("event_id")},
                   {"channel_count", channel_count},
                   {"start", {{"trace_period_id", start / per_trace},
                              {"integration_period_id", start % per_trace}}},
                   {"end_exclusive", {{"trace_period_id", end / per_trace},
                                      {"integration_period_id", end % per_trace}}},
                   {"period_count", count}, {"period_microseconds", integer(first, "period_microseconds")},
                   {"integration_per_trace", per_trace}, {"missing_by_server", missing}};
    const auto path = event_directory / "storage-complete.json";
    if (fs::exists(path)) require(read_json_file(path) == notice, "stored event result conflicts");
    else atomic_json(path, notice);
    return notice;
}

// Completion uses one UDP/JSON request and acknowledgment. A missing reply
// leaves the persisted result pending, so the background thread retries.
#ifdef _WIN32
using UdpSocket = SOCKET;
static constexpr UdpSocket INVALID_UDP_SOCKET = INVALID_SOCKET;
static void close_udp(UdpSocket socket) { closesocket(socket); }
#else
using UdpSocket = int;
static constexpr UdpSocket INVALID_UDP_SOCKET = -1;
static void close_udp(UdpSocket socket) { ::close(socket); }
#endif

class CompletionNotifier {
    fs::path root;
    std::string target_address, source_address;
    unsigned target_port;
    std::mutex mutex;
    std::condition_variable condition;
    bool stopped = false;
    bool pending = true;
    std::thread worker;

    bool send(const fs::path &event_directory, const json &notice) {
        UdpSocket socket = ::socket(AF_INET, SOCK_DGRAM, 0);
        if (socket == INVALID_UDP_SOCKET) throw std::runtime_error("completion UDP socket creation failed");
        try {
            sockaddr_in local{};
            local.sin_family = AF_INET;
            require(::inet_pton(AF_INET, source_address.c_str(), &local.sin_addr) == 1,
                    "invalid completion source IPv4");
            if (::bind(socket, reinterpret_cast<const sockaddr *>(&local), sizeof(local)))
                throw std::runtime_error("completion UDP bind failed");
            sockaddr_in target{};
            target.sin_family = AF_INET;
            target.sin_port = htons(static_cast<uint16_t>(target_port));
            require(::inet_pton(AF_INET, target_address.c_str(), &target.sin_addr) == 1,
                    "invalid completion target IPv4");
            const auto body = notice.dump();
            require(body.size() <= 1200, "completion JSON too long");
            if (::sendto(socket, body.data(), static_cast<int>(body.size()), 0,
                         reinterpret_cast<const sockaddr *>(&target), sizeof(target)) !=
                    static_cast<int>(body.size()))
                throw std::runtime_error("completion UDP send failed");
            fd_set readable;
            FD_ZERO(&readable);
            FD_SET(socket, &readable);
            timeval timeout{1, 0};
#ifdef _WIN32
            const int ready = ::select(0, &readable, nullptr, nullptr, &timeout);
#else
            const int ready = ::select(socket + 1, &readable, nullptr, nullptr, &timeout);
#endif
            if (ready <= 0) throw std::runtime_error("completion UDP acknowledgment timeout");
            std::array<char, 1201> reply{};
            sockaddr_in peer{};
            socklen_t peer_size = sizeof(peer);
            const int size = ::recvfrom(socket, reply.data(), 1200, 0,
                                        reinterpret_cast<sockaddr *>(&peer), &peer_size);
            require(size >= 0 && peer.sin_addr.s_addr == target.sin_addr.s_addr &&
                    peer.sin_port == target.sin_port, "completion UDP acknowledgment source mismatch");
            auto ack = json::parse(reply.data(), reply.data() + size);
            require(ack.is_object() && integer(ack, "version") == 1 && ack.at("command") == "ACK_RESULT" &&
                    ack.at("status") == "ACKNOWLEDGED" &&
                    ack.at("acquisition_id") == notice.at("acquisition_id") &&
                    ack.at("event_id") == notice.at("event_id"), "invalid backend completion acknowledgment");
            atomic_json(event_directory / "backend-acknowledged.json", ack);
            close_udp(socket);
            return true;
        } catch (...) {
            close_udp(socket);
            throw;
        }
    }

    void scan() {
        std::error_code error;
        if (!fs::is_directory(root, error)) return;
        for (const auto &acquisition : fs::directory_iterator(root)) {
            if (!acquisition.is_directory() || !valid_id(acquisition.path().filename().string())) continue;
            for (const auto &event : fs::directory_iterator(acquisition.path())) {
                if (!event.is_directory() || !valid_id(event.path().filename().string())) continue;
                try {
                    const auto ack_path = event.path() / "backend-acknowledged.json";
                    if (fs::exists(ack_path)) {
                        try {
                            const auto ack = read_json_file(ack_path);
                            if (integer(ack, "version") == 1 && ack.at("command") == "ACK_RESULT" &&
                                ack.at("status") == "ACKNOWLEDGED" &&
                                ack.at("acquisition_id") == acquisition.path().filename().string() &&
                                ack.at("event_id") == event.path().filename().string())
                                continue;
                        } catch (const std::exception &) {
                            // A malformed marker cannot suppress a durable notification;
                            // a valid reply atomically replaces it below.
                        }
                    }
                    auto notice = build_completion_notice(event.path());
                    if (notice) send(event.path(), *notice);
                } catch (const std::exception &caught) {
                    std::cerr << "completion notifier: " << caught.what() << '\n';
                }
            }
        }
    }

    void run() {
#ifdef _WIN32
        WSADATA winsock{};
        if (WSAStartup(MAKEWORD(2, 2), &winsock)) {
            std::cerr << "completion notifier: Winsock startup failed\n";
            return;
        }
#endif
        while (true) {
            try { scan(); }
            catch (const std::exception &caught) {
                std::cerr << "completion notifier scan: " << caught.what() << '\n';
            }
            std::unique_lock<std::mutex> lock(mutex);
            pending = false;
            condition.wait_for(lock, std::chrono::seconds(1), [&] { return stopped || pending; });
            if (stopped) break;
        }
#ifdef _WIN32
        WSACleanup();
#endif
    }

  public:
    CompletionNotifier(fs::path base, std::string target, unsigned port, std::string source)
        : root(std::move(base)), target_address(std::move(target)), source_address(std::move(source)),
          target_port(port), worker(&CompletionNotifier::run, this) {}
    ~CompletionNotifier() {
        { std::lock_guard<std::mutex> lock(mutex); stopped = true; }
        condition.notify_one();
        if (worker.joinable()) worker.join();
    }
    void wake() {
        { std::lock_guard<std::mutex> lock(mutex); pending = true; }
        condition.notify_one();
    }
};

struct ReceiverEndpoint {
    unsigned server = 0, lane = 0, port = 0;
    std::string address, allowed_source;
    ReceiverEndpoint(unsigned server_id, unsigned lane_id, unsigned tcp_port,
                     std::string bind_address, std::string source)
        : server(server_id), lane(lane_id), port(tcp_port), address(std::move(bind_address)),
          allowed_source(std::move(source)) {}
};

struct ActiveTransfer {
    std::unique_ptr<EventWriter> writer;
    std::chrono::steady_clock::time_point last_activity;
};

// Process one REQ message synchronously. The protocol grants only one message
// at a time per identity, so a second disk thread would add a context switch
// without creating more lane-level parallelism.
static std::pair<Bytes, bool> process_transfer(
        std::map<std::string, ActiveTransfer> &sessions, const std::string &identity,
        const Bytes &body, const fs::path &root, Shared &shared, unsigned server, unsigned lane) {
    auto it = sessions.find(identity);
    if (it == sessions.end()) {
        require(sessions.size() < MAX_SESSIONS_PER_PORT, "receiver port busy");
        require(body.size() <= 1204, "metadata exceeds limit");
        auto writer = std::unique_ptr<EventWriter>(new EventWriter(root, shared, server, lane));
        auto existing = writer->begin(body);
        if (!existing.empty()) return {std::move(existing), true};
        sessions.emplace(identity, ActiveTransfer{std::move(writer),
                                                  std::chrono::steady_clock::now()});
        return {Bytes{'O', 'K'}, false};
    }
    require(body.size() >= 4, "truncated message");
    if (read32(body.data()) == 2) {
        auto result = it->second.writer->finish(body);
        sessions.erase(it);
        return {std::move(result), true};
    }
    it->second.writer->block(body);
    it->second.last_activity = std::chrono::steady_clock::now();
    return {Bytes{'O', 'K'}, false};
}

// Each server/lane thread owns one ROUTER socket and performs its sequential
// validation and pwrite work before returning the per-message credit.
static void serve_port(void *context, const fs::path &root, const std::string &address, unsigned port,
                       unsigned server, unsigned lane,
                       const std::string &allowed_source, Shared &shared,
                       CompletionNotifier &notifier, const std::atomic<bool> &shutdown) {
    void *router = zmq_socket(context, ZMQ_ROUTER);
    if (!router) throw std::runtime_error("ZeroMQ ROUTER creation failed");
    std::map<std::string, ActiveTransfer> sessions;
    try {
        option(router, ZMQ_LINGER, 0); option(router, ZMQ_ROUTER_MANDATORY, 1);
        option(router, ZMQ_SNDHWM, 1); option(router, ZMQ_RCVHWM, 1);
        int64_t limit = MAX_MESSAGE;
        if (zmq_setsockopt(router, ZMQ_MAXMSGSIZE, &limit, sizeof(limit)))
            throw std::runtime_error("ZeroMQ maximum message size failed");
        auto endpoint = "tcp://" + address + ":" + std::to_string(port);
        if (zmq_bind(router, endpoint.c_str()))
            throw std::runtime_error(std::string("ZeroMQ bind: ") + zmq_strerror(zmq_errno()));
        std::cout << "storage server " << server << " lane " << lane << " listening on "
                  << endpoint << std::endl;
        while (!stopping && !shutdown.load(std::memory_order_relaxed)) {
            const auto now = std::chrono::steady_clock::now();
            for (auto it = sessions.begin(); it != sessions.end();) {
                if (now - it->second.last_activity >= std::chrono::seconds(120))
                    it = sessions.erase(it);
                else
                    ++it;
            }
            zmq_pollitem_t item{router, 0, ZMQ_POLLIN, 0};
            int rc = zmq_poll(&item, 1, 100);
            if (rc < 0) { if (zmq_errno() == EINTR) continue; throw std::runtime_error("ZeroMQ poll failed"); }
            if (!(item.revents & ZMQ_POLLIN)) continue;
            std::string identity, peer;
            Bytes body;
            size_t parts = 0;
            bool delimiter = false, more;
            do {
                ZmqMessage frame;
                if (zmq_msg_recv(&frame.value, router, 0) < 0)
                    throw std::runtime_error("ZeroMQ receive failed");
                auto *data = static_cast<const unsigned char *>(zmq_msg_data(&frame.value));
                auto size = zmq_msg_size(&frame.value);
                ++parts;
                if (parts == 1) identity.assign(reinterpret_cast<const char *>(data), size);
                else if (parts == 2) delimiter = size == 0;
                else if (parts == 3) {
                    if (auto *source = zmq_msg_gets(&frame.value, "Peer-Address")) peer = source;
                    if (size) body.assign(data, data + size);
                }
                more = zmq_msg_more(&frame.value) != 0;
            } while (more);
            if (parts != 3 || !delimiter || identity.empty()) {
                if (!identity.empty()) reply(router, identity, error_reply("invalid message envelope"));
                continue;
            }
            if (peer.empty() || peer != allowed_source) {
                reply(router, identity, error_reply("source address does not match receiver port"));
                continue;
            }
            try {
                auto result = process_transfer(sessions, identity, body, root, shared, server, lane);
                reply(router, identity, result.first);
                if (result.second) notifier.wake();
            } catch (const std::exception &error) {
                sessions.erase(identity);
                std::cerr << "receiver: " << error.what() << '\n';
                std::string reason = error.what();
                if (reason.size() > 990) reason.resize(990);
                reply(router, identity, error_reply(reason));
            }
        }
    } catch (...) {
        zmq_close(router); throw;
    }
    zmq_close(router);
}

static void serve(const fs::path &root, const std::vector<ReceiverEndpoint> &endpoints,
                  unsigned io_threads,
                  const std::string &notify_address, unsigned notify_port,
                  const std::string &notify_bind) {
    void *context = zmq_ctx_new();
    if (!context) throw std::runtime_error("ZeroMQ context creation failed");
    if (zmq_ctx_set(context, ZMQ_IO_THREADS, static_cast<int>(io_threads))) {
        zmq_ctx_term(context);
        throw std::runtime_error("ZeroMQ I/O thread configuration failed");
    }
    Shared shared;
    CompletionNotifier notifier(root, notify_address, notify_port, notify_bind);
    std::atomic<bool> shutdown{false};
    std::mutex error_mutex;
    std::exception_ptr first_error;
    std::vector<std::thread> receivers;
    try {
        for (unsigned index = 0; index < endpoints.size(); ++index) {
            receivers.emplace_back([&, index] {
                try {
                    const auto &endpoint = endpoints.at(index);
                    serve_port(context, root, endpoint.address, endpoint.port, endpoint.server,
                               endpoint.lane, endpoint.allowed_source, shared, notifier, shutdown);
                } catch (...) {
                    {
                        std::lock_guard<std::mutex> lock(error_mutex);
                        if (!first_error) first_error = std::current_exception();
                    }
                    shutdown.store(true, std::memory_order_relaxed);
                }
            });
        }
    } catch (...) {
        shutdown.store(true, std::memory_order_relaxed);
        for (auto &thread : receivers) if (thread.joinable()) thread.join();
        zmq_ctx_term(context);
        throw;
    }
    for (auto &thread : receivers) if (thread.joinable()) thread.join();
    zmq_ctx_term(context);
    if (first_error) std::rethrow_exception(first_error);
}

#ifndef STORAGE_RECEIVER_NO_MAIN
struct StorageConfig {
    fs::path directory;
    std::string backend_address, backend_bind_address;
    unsigned backend_port = 0, io_threads = 2;
    std::vector<ReceiverEndpoint> receivers;
};

static void require_yaml_keys(const YAML::Node &node, std::initializer_list<const char *> expected,
                              const char *section) {
    require(node.IsMap(), "storage YAML section must be a mapping");
    std::set<std::string> actual;
    for (const auto &entry : node) {
        require(entry.first.IsScalar(), "storage YAML key must be a scalar");
        actual.insert(entry.first.as<std::string>());
    }
    std::set<std::string> wanted;
    for (const auto *key : expected) wanted.insert(key);
    if (actual != wanted) throw std::runtime_error(std::string("invalid or missing keys in ") + section);
}

static StorageConfig load_storage_config(const fs::path &path) {
    const auto root = YAML::LoadFile(path.string());
    require_yaml_keys(root, {"storage", "backend"}, "root");
    const auto storage = root["storage"];
    const auto backend = root["backend"];
    require_yaml_keys(storage, {"directory", "ioThreads", "receivers"}, "storage");
    require_yaml_keys(backend, {"address", "port", "bindAddress"}, "backend");
    StorageConfig config;
    config.directory = storage["directory"].as<std::string>();
    config.io_threads = storage["ioThreads"].as<unsigned>();
    config.backend_address = backend["address"].as<std::string>();
    config.backend_port = backend["port"].as<unsigned>();
    config.backend_bind_address = backend["bindAddress"].as<std::string>();
    const auto receivers = storage["receivers"];
    require(receivers.IsSequence() && receivers.size() == STORAGE_PORT_COUNT,
            "receivers must contain exactly 28 entries");
    std::set<std::pair<unsigned, unsigned>> unique_endpoints;
    std::set<unsigned> unique_ports;
    std::map<std::string, unsigned> source_owners;
    std::map<unsigned, std::string> server_sources;
    for (const auto &receiver : receivers) {
        require_yaml_keys(receiver, {"serverId", "lane", "bindAddress", "port", "allowedSource"},
                          "storage.receivers[]");
        const auto server = receiver["serverId"].as<unsigned>();
        const auto lane = receiver["lane"].as<unsigned>();
        const auto address = receiver["bindAddress"].as<std::string>();
        const auto port = receiver["port"].as<unsigned>();
        const auto source = receiver["allowedSource"].as<std::string>();
        require(server < CACHE_NODE_COUNT && lane < LANE_COUNT && port >= 1024 && port <= 65535,
                "invalid receiver serverId, lane or port");
        require(unique_endpoints.insert({server, lane}).second && unique_ports.insert(port).second,
                "receiver server/lane and port must be unique");
        const auto owner = source_owners.emplace(source, server);
        require(owner.second || owner.first->second == server,
                "allowedSource cannot be shared by different servers");
        const auto server_source = server_sources.emplace(server, source);
        require(server_source.second || server_source.first->second == source,
                "both lanes for a server must share one allowedSource");
        config.receivers.push_back({server, lane, port, address, source});
    }

    in_addr parsed{};
    require(config.directory.is_absolute() && config.io_threads >= 1 && config.io_threads <= 32 &&
                ::inet_pton(AF_INET, config.backend_address.c_str(), &parsed) == 1 &&
                ::inet_pton(AF_INET, config.backend_bind_address.c_str(), &parsed) == 1 &&
                config.backend_port >= 1 && config.backend_port <= 65535,
            "invalid storage path, bind address or backend endpoint");
    for (const auto &receiver : config.receivers)
        require(::inet_pton(AF_INET, receiver.address.c_str(), &parsed) == 1 &&
                    ::inet_pton(AF_INET, receiver.allowed_source.c_str(), &parsed) == 1,
                "invalid data IPv4 address in storage.receivers");
    return config;
}

int main(int argc, char **argv) {
    try {
        const bool check_only = argc == 4 && std::string(argv[3]) == "--check-config";
        require((argc == 3 || check_only) && std::string(argv[1]) == "--config",
                "usage: tianlai-storage --config storage.yaml [--check-config]");
        const auto config = load_storage_config(argv[2]);
        if (check_only) {
            std::cout << "storage directory: " << config.directory << '\n';
            for (const auto &receiver : config.receivers)
                std::cout << "server " << receiver.server << " lane " << receiver.lane << ": "
                          << receiver.address << ':' << receiver.port << " <- "
                          << receiver.allowed_source << '\n';
            std::cout << "backend UDP: " << config.backend_bind_address << " -> "
                      << config.backend_address << ':' << config.backend_port << '\n';
            return 0;
        }
        fs::create_directories(config.directory);
        std::signal(SIGINT, stop_handler); std::signal(SIGTERM, stop_handler);
        serve(config.directory, config.receivers, config.io_threads,
              config.backend_address, config.backend_port,
              config.backend_bind_address);
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "[FATAL] " << error.what() << '\n';
        return 1;
    }
}
#endif
