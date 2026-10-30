#define STORAGE_RECEIVER_NO_MAIN
#include "../Storage/storage_receiver.cpp"
#include <cassert>

static void put32(Bytes &out, uint32_t value) {
    for (int i = 3; i >= 0; --i) out.push_back(static_cast<unsigned char>(value >> (i * 8)));
}
static void put64(Bytes &out, uint64_t value) {
    put32(out, static_cast<uint32_t>(value >> 32)); put32(out, static_cast<uint32_t>(value));
}
static Bytes metadata(const std::string &event, unsigned count = 1) {
    return framed_json(json{{"version", 1}, {"format", "TLSTORE1"}, {"event_id", event},
        {"acquisition_id", "acq"}, {"server_id", 3}, {"lane", 0}, {"port_count", 4},
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
    put32(result, static_cast<uint32_t>(body.size())); put32(result, ::crc32(0, body.data(), body.size()));
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
        EventWriter wrong_shard(root, shared, 0, 4);
        rejects([&] { wrong_shard.begin(metadata("wrong-shard")); }, "wrong storage port");
    }
    {
        EventWriter writer(root, shared);
        writer.begin(metadata("event"));
        EventWriter concurrent(root, shared);
        rejects([&] { concurrent.begin(metadata("other")); }, "already receiving");
        rejects([&] { writer.block(block(0, true)); }, "missing packet slot");
        auto bad_crc = block(0); bad_crc.back() = 1;
        rejects([&] { writer.block(bad_crc); }, "checksum mismatch");
        for (unsigned p = 0; p < 4; ++p) writer.block(block(p));
        auto result = writer.finish(end_message());
        assert(read32(result.data()) == result.size() - 4);
        assert(json::parse(result.begin() + 4, result.end()).at("status") == "STORED");
    }
    auto directory = root / "acq/event/server-3";
    json marker; std::ifstream(directory / "lane-0.stored.json") >> marker;
    assert(marker.at("missing_packets").at("0") == 17);
    std::ifstream in(directory / "port-0.bin", std::ios::binary);
    Bytes saved((std::istreambuf_iterator<char>(in)), {});
    auto expected = block(0);
    assert(saved == Bytes(expected.begin() + CHUNK_HEADER, expected.end()));
    {
        EventWriter conflict(root, shared);
        rejects([&] { conflict.begin(metadata("event", 2)); }, "metadata conflicts");
        assert(fs::exists(directory / "lane-0.stored.json"));
    }
    {
        EventWriter retry(root, shared);
        auto retry_result = retry.begin(metadata("event"));
        assert(read32(retry_result.data()) == retry_result.size() - 4);
        assert(json::parse(retry_result.begin() + 4, retry_result.end()).at("status") == "STORED");
    }
    assert(fs::exists(directory / "lane-0.stored.json"));
    // Exercise the actual worker's request/response ordering without a network socket.
    Responses responses;
    Session session("session-1");
    session.worker = std::thread(run_session, &session, std::cref(root), std::ref(shared),
                                 std::ref(responses), 0, 1);
    auto exchange = [&](Bytes message) {
        assert(session.put(std::move(message)));
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        while (std::chrono::steady_clock::now() < deadline) {
            {
                std::lock_guard<std::mutex> lock(responses.mutex);
                if (!responses.ready.empty()) {
                    auto response = std::move(responses.ready.front()); responses.ready.pop();
                    return response;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        throw std::runtime_error("session reply timeout");
    };
    assert(exchange(metadata("session")).body == Bytes({'O', 'K'}));
    for (unsigned p = 0; p < 4; ++p) assert(exchange(block(p)).body == Bytes({'O', 'K'}));
    auto result = exchange(end_message());
    assert(result.finished && json::parse(result.body.begin() + 4, result.body.end()).at("status") == "STORED");
    session.worker.join();
    assert(fs::exists(root / "acq/session/server-3/lane-0.stored.json"));
    std::cout << "C++ storage: shard mapping, validation, workers and idempotent retry passed\n";
}
