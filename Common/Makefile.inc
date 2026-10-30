CXX ?= g++
CPPFLAGS += -I../Common/include -DBOOST_BIND_GLOBAL_PLACEHOLDERS
CXXFLAGS ?= -O3 -g
CXXFLAGS += -std=c++14 -Wall -Wextra -Wpedantic -pthread
IXCAP_INCLUDE ?= /usr/local/include
IXCAP_LIB ?= /usr/local/lib
LDLIBS += -pthread -lz
# Override for installations without pkg-config, e.g. SPDLOG_LIBS='-lspdlog -lfmt'.
SPDLOG_CFLAGS ?= $(shell pkg-config --cflags spdlog 2>/dev/null)
SPDLOG_LIBS ?= $(shell pkg-config --libs spdlog 2>/dev/null || echo '-lspdlog -lfmt')
CPPFLAGS += $(SPDLOG_CFLAGS)
LDLIBS += $(SPDLOG_LIBS)
LDLIBS += -lyaml-cpp
ZMQ_CFLAGS ?= $(shell pkg-config --cflags libzmq 2>/dev/null)
ZMQ_LIBS ?= $(shell pkg-config --libs libzmq 2>/dev/null || echo '-lzmq')
CPPFLAGS += $(ZMQ_CFLAGS)
LDLIBS += $(ZMQ_LIBS)
ifeq ($(NO_IXCAP),1)
CPPFLAGS += -DSTORE_NO_IXCAP
else
CPPFLAGS += -I$(IXCAP_INCLUDE)
LDLIBS += -L$(IXCAP_LIB) -lixcap -lrt -ldl
endif
COMMON = yaml_config protocol receive_pool log_output receive_antenna trigger_control zmq_store send_store application
BUILD = build$(if $(filter 1,$(NO_IXCAP)),-simulation,)
OBJECTS = $(COMMON:%=$(BUILD)/%.o) $(BUILD)/entry.o
BIN = $(TARGET)$(if $(filter 1,$(NO_IXCAP)),-simulation,)
.PHONY: all clean
all: $(BIN)
$(BIN): $(OBJECTS)
	$(CXX) $(CXXFLAGS) $^ $(LDLIBS) -o $@
$(BUILD)/%.o: ../Common/src/%.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@
$(BUILD)/entry.o: main.cpp
	@mkdir -p $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@
clean:
	rm -rf build build-simulation $(TARGET) $(TARGET)-simulation
-include $(OBJECTS:.o=.d)
