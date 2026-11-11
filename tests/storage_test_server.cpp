#define STORAGE_RECEIVER_NO_MAIN
#include "../Storage/storage_receiver.cpp"

// Windows loopback wrapper: exercises the production receiver without requiring
// yaml-cpp on the development host. Linux production tests use storage.yaml.
int main(int argc, char **argv) {
    try {
        std::string directory, bind, notify_address, notify_bind;
        unsigned port = 0, receiver_count = 0, notify_port = 0;
        for (int i = 1; i + 1 < argc; i += 2) {
            const std::string name = argv[i], value = argv[i + 1];
            if (name == "--directory") directory = value;
            else if (name == "--bind") bind = value;
            else if (name == "--port") port = std::stoul(value);
            else if (name == "--receiver-count") receiver_count = std::stoul(value);
            else if (name == "--notify-address") notify_address = value;
            else if (name == "--notify-port") notify_port = std::stoul(value);
            else if (name == "--notify-bind") notify_bind = value;
            else throw std::runtime_error("unknown test argument");
        }
        require(!directory.empty() && receiver_count == STORAGE_PORT_COUNT,
                "invalid storage test arguments");
        std::vector<ReceiverEndpoint> endpoints;
        for (unsigned server = 0; server < CACHE_NODE_COUNT; ++server)
            for (unsigned lane = 0; lane < LANE_COUNT; ++lane)
                endpoints.push_back({server, lane, port + lane * CACHE_NODE_COUNT + server, bind,
                    "127.0.0." + std::to_string(1 + server + lane * CACHE_NODE_COUNT)});
        fs::create_directories(directory);
        std::signal(SIGINT, stop_handler);
        std::signal(SIGTERM, stop_handler);
        serve(directory, endpoints, 4, default_server_ids(),
              fs::path(directory) / "storage-status.json",
              notify_address, notify_port, notify_bind);
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "[FATAL] " << error.what() << '\n';
        return 1;
    }
}
