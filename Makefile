# Unified entry point for the three cache-node profiles.
PROFILES := F0-2 F3-10 F11-13
PROFILE ?= all
PYTHON ?= python3
export MAKE

ifeq ($(PROFILE),all)
SELECTED_PROFILES := $(PROFILES)
else
ifneq ($(words $(PROFILE)),1)
$(error PROFILE must be one of all, F0-2, F3-10 or F11-13)
endif
ifeq ($(filter $(PROFILE),$(PROFILES)),)
$(error Unknown PROFILE '$(PROFILE)'; use all, F0-2, F3-10 or F11-13)
endif
SELECTED_PROFILES := $(PROFILE)
endif

.PHONY: all production simulation storage clean-storage test clean help \
        $(addprefix production-,$(PROFILES)) \
        $(addprefix simulation-,$(PROFILES)) \
        $(addprefix clean-,$(PROFILES))

all: production

production: $(addprefix production-,$(SELECTED_PROFILES)) $(if $(filter all,$(PROFILE)),storage,)
simulation: $(addprefix simulation-,$(SELECTED_PROFILES))
clean: $(addprefix clean-,$(SELECTED_PROFILES)) $(if $(filter all,$(PROFILE)),clean-storage,)

storage:
	+$(MAKE) -C Storage

clean-storage:
	+$(MAKE) -C Storage clean

$(addprefix production-,$(PROFILES)):
	+$(MAKE) -C $(patsubst production-%,%,$@) NO_IXCAP=0

$(addprefix simulation-,$(PROFILES)):
	+$(MAKE) -C $(patsubst simulation-%,%,$@) NO_IXCAP=1

$(addprefix clean-,$(PROFILES)):
	+$(MAKE) -C $(patsubst clean-%,%,$@) clean

test:
	CXX="$(CXX)" $(PYTHON) tests/run.py

help:
	@printf '%s\n' \
	  'make production                 Build all three real-NIC programs (default).' \
	  'make simulation                 Build all three programs without ixcap.' \
	  'make production PROFILE=F3-10   Build one profile.' \
	  'make storage                    Build the C++ storage receiver.' \
	  'make test                       Run regression and simulation tests.' \
	  'make clean [PROFILE=F3-10]     Remove selected build outputs and binaries.' \
	  'Overrides: CXX, PYTHON, IXCAP_INCLUDE, IXCAP_LIB, SPDLOG_CFLAGS, SPDLOG_LIBS.'
