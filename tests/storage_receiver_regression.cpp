#define STORAGE_RECEIVER_NO_MAIN
#include "../Storage/storage_receiver.cpp"
#include <cassert>

static void put32(Bytes &out, uint32_t value) {
    for (int i = 3; i >= 0; --i) out.push_back(static_cast<unsigned char>(value >> (i * 8)));
}
static void put64(Bytes &out, uint64_t value) {
    put32(out, static_cast<uint32_t>(value >> 32)); put32(out, static_cast<uint32_t>(value));
}
static Bytes metadata(const std::string &event, unsigned lane = 0, unsigned count = 1) {
    return framed_json(json{{"version", 1}, {"format", "TLSTORE1"}, {"event_id", event},
        {"acquisition_id", "acq"}, {"server_id", 3}, {"lane", lane}, {"port_count", 4},
        {"channel_count", 384},
        {"ports", lane == 0 ? json{0, 2} : json{1, 3}},
        {"start_period", 100}, {"period_count", count}, {"integration_per_trace", 100},
        {"period_microseconds", 10000}, {"packet_bytes", 8848}});
}
static Bytes block(unsigned port, bool bad_missing = false) {
    Bytes body;
    put64(body, 100); put64(body, 1); put32(body, 8848); put32(body, 18);
    body.resize(FRAME_HEADER + 18 * 8848, 0);
    auto *raw = body.data() + FRAME_HEADER;
    raw[0] = 0xf0; raw[1] = 0; raw[2] = 1; // trace 1, integration 0
    raw[6] = (port % 2) * 18; raw[7] = 24 + port / 2;
    raw[8] = 0x5a; raw[16] = 0x33;
    if (bad_missing) body[FRAME_HEADER + 8848] = 1;
    Bytes result;
    put32(result, 1); put32(result, port); put32(result, 24 + port / 2);
    put32(result, (port % 2) * 18); put64(result, 100); put64(result, 1);
    put32(result, static_cast<uint32_t>(body.size())); put32(result, 0);
    result.insert(result.end(), body.begin(), body.end());
    return result;
}
static Bytes end_message() { Bytes value(40, 0); value[3] = 2; return value; }
template <class Fn> static void rejects(Fn fn, const std::string &expected) {
    try { fn(); throw std::runtime_error("invalid input accepted"); }
    catch (const std::exception &e) { assert(std::string(e.what()).find(expected) != std::string::npos); }
}

int main(int argc, char **argv) {
    assert(argc == 2);
    const fs::path root(argv[1]);
    fs::create_directories(root);
    Shared shared;
    {
        EventWriter wrong_endpoint(root, shared, 0, 0);
        rejects([&] { wrong_endpoint.begin(metadata("wrong-endpoint")); }, "wrong storage port");
    }
    {
        EventWriter writer(root, shared, 3, 0);
        EventWriter lane1(root, shared, 3, 1);
        writer.begin(metadata("event", 0));
        lane1.begin(metadata("event", 1));
        EventWriter concurrent(root, shared);
        rejects([&] { concurrent.begin(metadata("other")); }, "already receiving");
        rejects([&] { writer.block(block(0, true)); }, "missing packet slot");
        auto bad_reserved = block(0); bad_reserved[39] = 1;
        rejects([&] { writer.block(bad_reserved); }, "invalid chunk range/layout");
        for (unsigned p : {0U, 2U}) writer.block(block(p));
        for (unsigned p : {1U, 3U}) lane1.block(block(p));
        auto result = writer.finish(end_message());
        auto lane1_result = lane1.finish(end_message());
        assert(read32(result.data()) == result.size() - 4);
        assert(read32(lane1_result.data()) == lane1_result.size() - 4);
        assert(json::parse(result.begin() + 4, result.end()).at("status") == "STORED");
    }
    auto directory = root / "acq/event/server-3";
    json marker; std::ifstream(directory / "lane-0.stored.json") >> marker;
    assert(fs::exists(directory / "lane-1.stored.json"));
    assert(marker.at("missing_packets").at("0") == 17);
    std::ifstream in(directory / "port-0.bin", std::ios::binary);
    Bytes saved((std::istreambuf_iterator<char>(in)), {});
    auto expected = block(0);
    assert(saved == Bytes(expected.begin() + CHUNK_HEADER, expected.end()));
    {
        EventWriter conflict(root, shared);
        rejects([&] { conflict.begin(metadata("event", 0, 2)); }, "metadata conflicts");
        assert(fs::exists(directory / "lane-0.stored.json"));
    }
    {
        EventWriter retry(root, shared);
        auto retry_result = retry.begin(metadata("event"));
        assert(read32(retry_result.data()) == retry_result.size() - 4);
        assert(json::parse(retry_result.begin() + 4, retry_result.end()).at("status") == "STORED");
    }
    assert(fs::exists(directory / "lane-0.stored.json"));
    // A 384-channel event requires both durable lanes from all 14 servers.
    const auto complete = root / "acq/complete";
    for (unsigned server = 0; server < CACHE_NODE_COUNT; ++server) {
        const auto node = complete / ("server-" + std::to_string(server));
        fs::create_directories(node);
        for (unsigned lane = 0; lane < LANE_COUNT; ++lane) {
            const auto prefix = "lane-" + std::to_string(lane);
            json meta = {{"version", 1}, {"format", "TLSTORE1"}, {"acquisition_id", "acq"},
                         {"event_id", "complete"}, {"server_id", server}, {"lane", lane},
                         {"channel_count", 384},
                         {"start_period", 100}, {"period_count", 1},
                         {"integration_per_trace", 100}, {"period_microseconds", 10000}};
            json stored = {{"version", 1}, {"status", "STORED"}, {"acquisition_id", "acq"},
                           {"event_id", "complete"}, {"server_id", server}, {"lane", lane},
                           {"missing_packets", {{std::to_string(lane), lane + 1}}}};
            atomic_json(node / (prefix + ".meta.json"), meta);
            atomic_json(node / (prefix + ".stored.json"), stored);
        }
    }
    const auto notice = build_completion_notice(complete);
    assert(notice && notice->at("status") == "STORED");
    assert(notice->at("channel_count") == 384);
    assert(notice->at("missing_by_server").at("0") == 3);
    assert(fs::exists(complete / "storage-complete.json"));
    // A 288-channel event is complete after both lanes from servers 0-10.
    const auto complete288 = root / "acq/complete-288";
    for (unsigned server = 0; server < 11; ++server) {
        const auto node = complete288 / ("server-" + std::to_string(server));
        fs::create_directories(node);
        for (unsigned lane = 0; lane < LANE_COUNT; ++lane) {
            const auto prefix = "lane-" + std::to_string(lane);
            json meta = {{"version", 1}, {"format", "TLSTORE1"}, {"acquisition_id", "acq"},
                         {"event_id", "complete-288"}, {"server_id", server}, {"lane", lane},
                         {"channel_count", 288}, {"start_period", 100}, {"period_count", 1},
                         {"integration_per_trace", 100}, {"period_microseconds", 10000}};
            json stored = {{"version", 1}, {"status", "STORED"}, {"acquisition_id", "acq"},
                           {"event_id", "complete-288"}, {"server_id", server}, {"lane", lane},
                           {"missing_packets", {{std::to_string(lane), 0}}}};
            atomic_json(node / (prefix + ".meta.json"), meta);
            atomic_json(node / (prefix + ".stored.json"), stored);
        }
    }
    const auto notice288 = build_completion_notice(complete288);
    assert(notice288 && notice288->at("channel_count") == 288);
    assert(notice288->at("missing_by_server").size() == 11);
    // The lane thread processes metadata, blocks and END without a second worker.
    std::map<std::string, ActiveTransfer> sessions;
    auto response = process_transfer(sessions, "client", metadata("session"), root, shared, 3, 0);
    assert(response.first == Bytes({'O', 'K'}) && !response.second);
    for (unsigned p : {0U, 2U}) {
        response = process_transfer(sessions, "client", block(p), root, shared, 3, 0);
        assert(response.first == Bytes({'O', 'K'}) && !response.second);
    }
    response = process_transfer(sessions, "client", end_message(), root, shared, 3, 0);
    assert(response.second && sessions.empty());
    assert(json::parse(response.first.begin() + 4, response.first.end()).at("status") == "STORED");
    response = process_transfer(sessions, "retry", metadata("session"), root, shared, 3, 0);
    assert(response.second && sessions.empty());
    std::cout << "C++ storage: channel modes, dual-lane mapping and idempotent retry passed\n";
}
