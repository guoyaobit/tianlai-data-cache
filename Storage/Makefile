CXX ?= g++
CPPFLAGS += -DBOOST_BIND_GLOBAL_PLACEHOLDERS
CXXFLAGS ?= -O3 -g
CXXFLAGS += -std=c++17 -Wall -Wextra -Wpedantic -pthread
ZMQ_CFLAGS ?= $(shell pkg-config --cflags libzmq 2>/dev/null)
ZMQ_LIBS ?= $(shell pkg-config --libs libzmq 2>/dev/null || echo '-lzmq')
YAML_CFLAGS ?= $(shell pkg-config --cflags yaml-cpp 2>/dev/null)
YAML_LIBS ?= $(shell pkg-config --libs yaml-cpp 2>/dev/null || echo '-lyaml-cpp')
CPPFLAGS += $(ZMQ_CFLAGS) $(YAML_CFLAGS)
LDLIBS += $(ZMQ_LIBS) $(YAML_LIBS) -lz -pthread

.PHONY: all clean
all: tianlai-storage

tianlai-storage: storage_receiver.cpp Makefile
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< $(LDLIBS) -o $@

clean:
	rm -f tianlai-storage
