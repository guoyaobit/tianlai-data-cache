#ifndef STORE_LOGOUT_H_
#define STORE_LOGOUT_H_
#include <string>
enum LogLevel { DEBUG, INFO, WARN, ERRO, FATAL, START };
void initLogOut(const std::string &path, bool debug);
void logOut(const std::string &message, LogLevel level);
#define LOG(message, level) logOut((message), (level))
#endif
