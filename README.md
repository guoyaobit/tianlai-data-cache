# Tianlai Raw Data Cache

Author: [guoyaobit](https://github.com/guoyaobit)

Tianlai keeps a rolling history of raw FPGA data in memory. A trigger selects which cache servers and integration periods to save. The storage server writes the data and reports the outcome, including missing packet counts.

## Quick start

For a first installation, follow the [deployment guide](DEPLOYMENT_GUIDE.md) and [Ansible instructions](deploy/ansible/README.md). Once the services are running, use these commands from a node with the client tools installed.

```bash
tlcache --watch 2                       # Monitor cache nodes and storage progress
tltrigger status --288 --json            # Check whether the 288-channel group can capture
tltrigger 0.5 --288 --json               # Save the latest 0.5 seconds and wait
tltrigger 0.5 --288 --period 7000000      # Save 0.5 seconds from a known integration period
tltrigger capture.json                   # Submit a JSON capture request and wait
```

The 288-channel preset selects the first 11 cache servers. With no preset or explicit server selection, a capture uses the configured default node group. A JSON request can select individual servers, use absolute start and end periods, or specify separate time ranges for different frequency bands. Ranges include the start and exclude the end. Requested data must still be in the selected servers' settled cache. Capture duration is aligned to the integration period, which defaults to 200 µs, with a maximum of 50 seconds.

`COMPLETED` confirms that the selected data was written and its cache slots were released. Check the event's missing packet counts to assess data completeness.

| If you need to… | Read |
|---|---|
| Submit triggers, handle completion notices, or read stored data | [Trigger and data format](PROTOCOL.md) |
| Integrate a display or control application | [Display interface](DISPLAY_INTERFACE.md) |
| Install, configure, or troubleshoot the services | [Deployment guide](DEPLOYMENT_GUIDE.md) |
| Deploy or update nodes with Ansible | [Ansible deployment](deploy/ansible/README.md) |
| Work on the cache or storage code | [Code structure](CODE_STRUCTURE.md) · [Internal protocol](INTERNAL_PROTOCOL.md) |

## Data flow

1. Each receive port collects FPGA UDP packets with ixcap and writes them into a ring indexed by absolute integration period. Two 50-second banks normally retain about 100 seconds per port.
2. The controller accepts TCP/JSON requests on the management network. It freezes the selected cache slots in place while receive threads continue writing to available slots.
3. Each selected node sends its data over two ZeroMQ TCP lanes that share one physical transmit port. Lane 0 carries even receive ports, and lane 1 carries odd receive ports. The storage service writes the data and acknowledges it before the controller releases the frozen slots.
4. Stored packets retain their 16-byte application header and packed samples. A bitmap identifies missing packet slots, which are filled with zeros when building the saved output.

Production uses separate management and storage networks. The controller and storage receiver run on the same storage host as `tianlai-controller.service` and `tianlai-storage.service`. The storage receiver uses one 100 GbE port with two listeners per cache server. The [internal protocol](INTERNAL_PROTOCOL.md) describes data transfer and persistence acknowledgments.

## Cache profiles

Server IDs are zero-based. For example, server ID 0 is `tianlaistore1`. All three profiles share the implementation in `Common/`.

| Profile | Server IDs | Receive ports per server | Packet ID | Boards per port | Raw payload per packet |
|---|---|---:|---|---|---:|
| `F0-2/` | 0–2 | 8 | `serverId*8+port` | 0–35 | 2304 B |
| `F3-10/` | 3–10 | 4 | `24+(serverId-3)*2+port/2` | 0–17 on even ports, 18–35 on odd ports | 8832 B |
| `F11-13/` | 11–13 | 4 | `2*((serverId-11)*4+port)` and the next ID | 36–47, two packets per board | 8256 B |

The first 11 servers use the input packet format from `tianlai-A288B288-20260917`. The final three receive remote station data. See the [remote station profile](F11-13/README.md) for the frequency mapping and padding details.

## Build and verify

The cache requires Linux, a C++14 compiler, Boost headers, yaml-cpp, libzmq, spdlog with fmt, and the ixcap SDK for production reception. The C++17 storage receiver also requires nlohmann-json. The controller and client tools use Python 3.10 or later.

Run these commands from the repository root.

```bash
make -j4 production                  # Build all profiles and the storage receiver
make -j4 production PROFILE=F3-10    # Build one cache profile
make -j4 storage                     # Build only the storage receiver
make -j4 simulation                  # Build loopback binaries without ixcap
make test                            # Build and run simulation checks
```

Set `IXCAP_INCLUDE` and `IXCAP_LIB` if the SDK is installed outside the default paths. Use `make help` to list build targets. Tests also require Python pyzmq and a compiler with ASan and UBSan support.

See the [deployment guide](DEPLOYMENT_GUIDE.md) for manual launch commands, YAML configuration, NIC and CPU placement, memory sizing, recovery, and hardware verification. Coordinated captures require a common integration counter origin across nodes. A full 50-second capture in 384-channel mode uses about 2.37 TB, so check available storage and sustained write throughput before production use.
