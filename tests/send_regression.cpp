#include "zmq_store.h"
#include <cassert>
#include <chrono>
#include <iostream>
#include <thread>

int main() {
    const uint64_t rate = 64000000;
    const size_t bytes = 8192, packets = 80;
    StorePacer pacer(rate);
    auto measure = [&]() {
        auto start = std::chrono::steady_clock::now();
        for (size_t i = 0; i < packets; ++i) pacer.wait(bytes);
        double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
        double minimum = double(packets - 1) * bytes * 8 / rate;
        assert(seconds >= minimum * .99);
        return seconds;
    };
    measure();
    // A pause must not accumulate credit which is then emitted as a line-rate burst.
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    measure();
    std::cout << "ZeroMQ pacer: application byte rate and no idle-credit burst passed\n";
}
