#include "functions.h"
#include "log_output.h"
#include "protocol.h"
#include <arpa/inet.h>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

namespace {
class Socket {
  public:
    explicit Socket(int value = -1) : fd(value) {}
    ~Socket() { if (fd >= 0) close(fd); }
    Socket(const Socket &) = delete;
    Socket &operator=(const Socket &) = delete;
    int get() const { return fd; }
  private:
    int fd;
};

bool receiveExact(int fd, unsigned char *data, size_t size) {
    while (size) {
        const auto received = recv(fd, data, size, 0);
        if (received > 0) {
            data += received;
            size -= static_cast<size_t>(received);
        } else if (received == 0) {
            return false;
        } else if (errno != EINTR) {
            return false;
        }
    }
    return true;
}

bool sendExact(int fd, const unsigned char *data, size_t size) {
    while (size) {
        const auto sent = send(fd, data, size, MSG_NOSIGNAL);
        if (sent > 0) {
            data += sent;
            size -= static_cast<size_t>(sent);
        } else if (sent < 0 && errno == EINTR) {
            continue;
        } else {
            return false;
        }
    }
    return true;
}

uint32_t frameLength(const unsigned char *header) {
    return (uint32_t(header[0]) << 24) | (uint32_t(header[1]) << 16) |
           (uint32_t(header[2]) << 8) | header[3];
}

bool sendFrame(int fd, const std::string &body) {
    if (body.empty() || body.size() > MAX_CONTROL_BYTES)
        return false;
    const uint32_t size = static_cast<uint32_t>(body.size());
    const unsigned char header[4] = {
        static_cast<unsigned char>(size >> 24), static_cast<unsigned char>(size >> 16),
        static_cast<unsigned char>(size >> 8), static_cast<unsigned char>(size)};
    return sendExact(fd, header, sizeof(header)) &&
           sendExact(fd, reinterpret_cast<const unsigned char *>(body.data()), body.size());
}
} // namespace

// Serve one length-prefixed TCP/JSON request per authenticated connection.
void InitControlThread(CacheRuntime *runtime) {
    Socket listener(socket(AF_INET, SOCK_STREAM, 0));
    if (listener.get() < 0)
        throw std::runtime_error("control TCP socket failed");
    int enabled = 1;
    setsockopt(listener.get(), SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
    sockaddr_in address{}, allowed{};
    address.sin_family = AF_INET;
    address.sin_port = htons(runtime->config->controlPort);
    inet_pton(AF_INET, runtime->config->controlBindIp.c_str(), &address.sin_addr);
    inet_pton(AF_INET, runtime->config->controlPeerIp.c_str(), &allowed.sin_addr);
    if (bind(listener.get(), reinterpret_cast<sockaddr *>(&address), sizeof(address)) ||
        listen(listener.get(), 16))
        throw std::runtime_error("control TCP listen failed");
    LOG("TCP/JSON control ready, acquisition=" + runtime->config->acquisitionId, START);
    while (runtime->flags->ifControl) {
        pollfd poller{listener.get(), POLLIN, 0};
        if (poll(&poller, 1, 100) <= 0)
            continue;
        sockaddr_in peer{};
        socklen_t peerSize = sizeof(peer);
        Socket connection(accept(listener.get(), reinterpret_cast<sockaddr *>(&peer), &peerSize));
        if (connection.get() < 0 || peer.sin_addr.s_addr != allowed.sin_addr.s_addr)
            continue;
        timeval timeout{5, 0};
        setsockopt(connection.get(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(connection.get(), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        unsigned char header[4];
        if (!receiveExact(connection.get(), header, sizeof(header)))
            continue;
        const uint32_t size = frameLength(header);
        if (!size || size > MAX_CONTROL_BYTES)
            continue;
        std::string request(size, '\0');
        if (!receiveExact(connection.get(), reinterpret_cast<unsigned char *>(&request[0]), size))
            continue;
        sendFrame(connection.get(), ProcessControl(*runtime, request));
    }
}
