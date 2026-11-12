#include "log_output.h"
#include <chrono>
#include <cstdlib>
#include <memory>
#include <vector>
#include <spdlog/async.h>
#include <spdlog/async_logger.h>
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
    // Keep console and file I/O away from the packet receive threads. The
    // bounded queue drops the oldest log entry instead of stalling live input
    // if journald or the disk is temporarily slow.
    std::vector<spdlog::sink_ptr> sinks;
    auto consoleSink = std::make_shared<spdlog::sinks::stderr_color_sink_mt>();
    // Journald already supplies time, host and process metadata. Avoid repeating
    // them while retaining full timestamps for an interactive terminal.
    if (std::getenv("JOURNAL_STREAM"))
        consoleSink->set_pattern("[%l] %v");
    else
        consoleSink->set_pattern("%Y-%m-%d %H:%M:%S.%e [%l] [T%t] %v");
    sinks.push_back(consoleSink);
    if (!path.empty() && path != "-") {
        constexpr size_t maxBytes = 50U * 1024 * 1024;
        constexpr size_t backupFiles = 5;
        auto fileSink =
            std::make_shared<spdlog::sinks::rotating_file_sink_mt>(path, maxBytes, backupFiles);
        fileSink->set_pattern("%Y-%m-%d %H:%M:%S.%e [%l] [T%t] %v");
        sinks.push_back(fileSink);
    }
    constexpr size_t queueSize = 8192;
    spdlog::init_thread_pool(queueSize, 1);
    logger = std::make_shared<spdlog::async_logger>(
        "tianlai-store", sinks.begin(), sinks.end(), spdlog::thread_pool(),
        spdlog::async_overflow_policy::overrun_oldest);
    logger->set_level(debug ? spdlog::level::debug : spdlog::level::info);
    logger->flush_on(spdlog::level::warn);
    spdlog::register_logger(logger);
    spdlog::flush_every(std::chrono::seconds(3));
}

void logOut(const std::string &message, LogLevel level) {
    if (logger)
        logger->log(severity(level), "{}", message);
}
