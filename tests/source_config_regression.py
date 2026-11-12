#!/usr/bin/env python3
"""Run lightweight source, logging, protocol, and YAML configuration checks."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def check_zmq_logging():
    source = read("Common/src/zmq_store.cpp")
    header = read("Common/include/zmq_store.h")
    assert '" connecting: source="' in source
    assert '" connecting: interface="' not in source
    assert '" destination="' in source
    assert '" asynchronous connect started: destination="' in source
    assert source.count('" connected: destination="') == 1
    assert "if (!connectionReadyLogged)" in source
    assert "bool connectionReadyLogged = false" in header


def check_receive_logging_and_marker():
    source = read("Common/src/receive_antenna.cpp")
    log_source = read("Common/src/log_output.cpp")
    protocol_header = read("Common/include/protocol.h")
    protocol_source = read("Common/src/protocol.cpp")
    storage_source = read("Storage/storage_receiver.cpp")
    storage_test = read("tests/storage_receiver_regression.cpp")

    assert "bool &firstPacketLogged" in source
    assert source.count("bool firstPacketLogged = false") == 2
    assert source.count("bool firstUdpLogged = false") == 2
    assert "bool firstUnparsedFrameLogged = false" in source
    assert '" first packet: id="' in source
    assert '" first frame parse failed: bytes="' in source
    assert '" first accepted: id="' in source
    for field in ("board=", "period=", "bytes="):
        assert field in source
    assert "logFirstUdpPacket(p, data, bytes, frameBytes, firstUdpLogged)" in source
    assert "receive(absolutePeriod, slot, h.ifTrace, data)" in source
    assert "!firstPacketLogged" in source
    assert "firstPacketLogged = true" in source
    assert 'std::getenv("JOURNAL_STREAM")' in log_source
    assert 'consoleSink->set_pattern("[%l] %v")' in log_source
    assert log_source.count("%Y-%m-%d %H:%M:%S.%e") == 2

    assert "unsigned valid" not in protocol_header
    assert "h.valid" not in protocol_source
    assert "h.valid" not in source
    assert '" valid="' not in source
    assert "raw[0] >> 4" not in storage_source
    assert "raw[0] = 0x00" in storage_test


def check_receive_performance_guards():
    application = read("Common/src/application.cpp")
    receive_pool = read("Common/src/receive_pool.cpp")
    assert "allocatePortBuffer(index, port, c)" in application
    assert "pthread_setaffinity_np" in application
    assert application.index("pthread_setaffinity_np") < application.index(
        "new ReceiveRingBuffer"
    )
    assert "if (latest > previousLatest)" in receive_pool
    assert "detectPacketLoss(now, previousLatest)" in receive_pool


def check_read_only_management_queries():
    control = read("Common/src/control_tcp.cpp")
    assert "sameManagementSubnet" in control
    assert "isReadOnlyQuery" in control
    assert 'command == "QUERY_STATUS" || command == "QUERY_PORT"' in control
    assert "peer.sin_addr.s_addr == allowed.sin_addr.s_addr" in control


def check_simplified_storage_state():
    storage = read("Storage/storage_receiver.cpp")
    assert '"lane-" + std::to_string(lane) + ".json"' in storage
    assert 'event_directory / "event.json"' in storage
    for old_name in ('.meta.json', '.stored.json', 'storage-complete.json',
                     'backend-acknowledged.json'):
        assert old_name not in storage


def check_simulation_config():
    configs = sorted((ROOT / "F0-2").glob("server_*.yaml"))
    configs += sorted((ROOT / "F3-10").glob("server_*.yaml"))
    configs += sorted((ROOT / "F11-13").glob("server_*.yaml"))
    assert len(configs) == 14

    simulation_port = re.compile(r"^\s+#?\s*simulationPort\d+:", re.MULTILINE)
    for path in configs:
        assert not simulation_port.search(path.read_text(encoding="utf-8")), path

    assert "simulationPort" not in read("deploy/ansible/templates/server.yaml.j2")
    assert "node[simulationKey] ? number(node, simulationKey, 1024, 65535) : 0" in read(
        "Common/src/yaml_config.cpp"
    )
    assert "set simulationPort for every receive port when using --simulate" in read(
        "Common/src/application.cpp"
    )


def check_yaml_style():
    files = sorted((ROOT / "F0-2").glob("*.yaml"))
    files += sorted((ROOT / "F3-10").glob("*.yaml"))
    files += sorted((ROOT / "F11-13").glob("*.yaml"))
    files += [ROOT / "Storage/storage.yaml"]
    files += [
        ROOT / "deploy/ansible/templates/recv.yaml.j2",
        ROOT / "deploy/ansible/templates/server.yaml.j2",
        ROOT / "deploy/ansible/templates/storage.yaml.j2",
    ]

    standalone_comment = re.compile(r"^ {2,}# ", re.MULTILINE)
    inline_comment = re.compile(r"^\s*[^#\s][^\n]*  # ", re.MULTILINE)
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not standalone_comment.search(text), path
        assert inline_comment.search(text), path
        assert "recvCfg:" not in text, path
        assert "serverCfg:" not in text, path

    for path in [ROOT / "Storage/storage.yaml", ROOT / "deploy/ansible/templates/storage.yaml.j2"]:
        text = path.read_text(encoding="utf-8")
        assert "storage:" not in text, path
        assert "backend:" not in text, path
        keys = (
            "directory:", "statusFile:", "ioThreads:", "expectedServerIds:", "receivers:",
            "backendAddress:", "backendPort:", "backendBindAddress:",
        )
        for key in keys:
            assert re.search(rf"^{key}", text, re.MULTILINE), (path, key)


if __name__ == "__main__":
    check_zmq_logging()
    check_receive_logging_and_marker()
    check_receive_performance_guards()
    check_read_only_management_queries()
    check_simplified_storage_state()
    check_simulation_config()
    check_yaml_style()
    print("source and configuration regression checks passed")
