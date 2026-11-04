#include "functions.h"
#include "log_output.h"
#include <arpa/inet.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

// Receive authenticated controller datagrams and return bounded JSON replies.
void InitControlThread(CacheRuntime *runtime) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        throw std::runtime_error("control socket failed");
    struct Close {
        int fd;
        ~Close() {
            close(fd);
        }
    } closeFd{fd};
    sockaddr_in addr{}, allowed{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(runtime->config->controlPort);
    inet_pton(AF_INET, runtime->config->controlBindIp.c_str(), &addr.sin_addr);
    inet_pton(AF_INET, runtime->config->controlPeerIp.c_str(), &allowed.sin_addr);
    if (bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)))
        throw std::runtime_error("control bind failed");
    LOG("UDP control ready, acquisition=" + runtime->config->acquisitionId, START);
    while (runtime->flags->ifControl) {
        pollfd poller{fd, POLLIN, 0};
        if (poll(&poller, 1, 100) <= 0)
            continue;
        char buffer[MAX_CONTROL_BYTES + 1];
        sockaddr_in peer{};
        socklen_t size = sizeof(peer);
        auto bytes = recvfrom(fd, buffer, sizeof(buffer), MSG_TRUNC,
                              reinterpret_cast<sockaddr *>(&peer), &size);
        if (bytes < 0 || bytes > ssize_t(MAX_CONTROL_BYTES) ||
            peer.sin_addr.s_addr != allowed.sin_addr.s_addr)
            continue;
        auto response = ProcessControl(*runtime, std::string(buffer, size_t(bytes)));
        if (response.size() <= MAX_CONTROL_BYTES)
            sendto(fd, response.data(), response.size(), 0, reinterpret_cast<sockaddr *>(&peer),
                   size);
    }
}
