#include "log_output.h"
#include <chrono>
#include <memory>
#include <vector>
#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

namespace {
std::shared_ptr<spdlog::logger> logger;

spdlog::level::level_enum severity(LogLevel level) {
    switch (level) {
    case DEBUG:
        return spdlog::level::debug;
    case INFO:
    case START:
        return spdlog::level::info;
    case WARN:
        return spdlog::level::warn;
    case ERRO:
        return spdlog::level::err;
    case FATAL:
        return spdlog::level::critical;
    }
    return spdlog::level::err;
}
} // namespace

void initLogOut(const std::string &path, bool debug) {
    // Initialise before starting receive and control threads; the mt sinks are
    // safe for concurrent writers. File rotation bounds long-running log growth.
    std::vector<spdlog::sink_ptr> sinks;
    sinks.push_back(std::make_shared<spdlog::sinks::stderr_color_sink_mt>());
    if (!path.empty() && path != "-") {
        constexpr size_t maxBytes = 50U * 1024 * 1024;
        constexpr size_t backupFiles = 5;
        sinks.push_back(
            std::make_shared<spdlog::sinks::rotating_file_sink_mt>(path, maxBytes, backupFiles));
    }
    logger = std::make_shared<spdlog::logger>("tianlai-store", sinks.begin(), sinks.end());
    logger->set_pattern("%Y-%m-%d %H:%M:%S.%e [%l] [thread %t] %v");
    logger->set_level(debug ? spdlog::level::debug : spdlog::level::info);
    logger->flush_on(spdlog::level::warn);
    spdlog::register_logger(logger);
    spdlog::flush_every(std::chrono::seconds(3));
}

void logOut(const std::string &message, LogLevel level) {
    if (logger)
        logger->log(severity(level), "{}", message);
}
