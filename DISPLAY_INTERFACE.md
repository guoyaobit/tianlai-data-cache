# Display Control Interface

本文供显控开发人员使用。显控节点由部署程序安装 `tlcache` 和 `tltrigger`，通过管理网连接控制器 `172.0.0.142:18999`。显控程序可执行命令并读取标准输出中的 JSON；如需直接连接，TCP 报文见第 5 节。

| 操作 | 命令 |
|---|---|
| 查询节点和存储状态 | `tlcache --json` |
| 查询是否可以触发 | `tltrigger status --288 --json` |
| 触发并等待完成 | `tltrigger 3 --288 --json` |
| 仅提交触发 | `tltrigger capture 3 --288 --json` |
| 启停节点 | `tlcache start 1-3 --json`、`tlcache stop 1-3 --yes --json` |

以上 `3` 是保存 3 秒；`--288` 选择 288 路，省略时选择 384 路。时长范围为 0.1～50 秒，步长 0.1 秒，例如 `tltrigger 0.1 --288 --json`。

## 1. 查看缓存和存储状态

```bash
tlcache --json             # 存储状态 + 各缓存节点状态
tlcache storage --json     # 只查存储服务器，返回更快
```

`tlcache --json` 的顶层结构为：

```json
{
  "storage": {"state":"RECEIVING_AND_WRITING","event_written_bytes":53267040000,"event_expected_bytes":106534080000},
  "cache_nodes": [{"server_id":0,"runtime_state":"CACHING_DATA","receiving_ports":8,"receive_ports":8,"lost_packets":0}]
}
```

示例仅列常用字段。`server_id:0` 对应 `tianlaistore1`，依次到 `server_id:13` 对应 `tianlaistore14`。

| 字段 | 显示内容 |
|---|---|
| `storage.state` | 存储状态；`RECEIVING_AND_WRITING` 为正在写盘，`WAITING_FOR_DATA` 为等待数据 |
| `storage.event_written_bytes / event_expected_bytes` | 本次事件的估算写盘进度；重传时可能重复计数 |
| `storage.event_completed_lanes / event_expected_lanes` | 已完成 / 应完成的传输通道 |
| `storage.last_error` | 最近错误 |
| `cache_nodes[].runtime_state`、`uptime_ms` | 节点运行阶段和运行时间 |
| `receiving_ports / receive_ports` | 最近 2 秒有数据的网口数 / 总网口数 |
| `inactive_ports`、`lost_by_port` | 无数据网口和各口累计缺包 |

节点停止或异常时，`tlcache --json` 可能返回非零退出码，但标准输出仍包含状态 JSON；显控应读取并显示异常节点。终端人工查看可直接运行 `tlcache`；持续刷新使用 `tlcache --watch 2`。

## 2. 查询可触发范围

```bash
tltrigger status --288 --json
```

回复中的 `status` 为 `READY` 且 `accept_trigger` 为 `true` 时可触发。`oldest_period` 到 `settled_end` 是所有参与节点共同可用的绝对积分周期范围，包含起点、不包含终点。`--288` 使用前 11 台缓存服务器；省略时为 384 路、14 台服务器。

## 3. 触发并确认写盘完成

最简单的方式，保存最近 3 秒的 288 路数据，并等待最终结果：

```bash
tltrigger 3 --288 --json
```

显控程序不想一直等待命令时，分两步执行：

```bash
tltrigger capture 3 --288 --json
tltrigger event EVENT_ID --json
```

第一条返回 `PREPARING` 和 `event_id`。把返回的编号填入第二条，定期查询，直到：

```json
{"status":"PREPARING","event_id":"event-example-001"}
```

| `status` | 含义 |
|---|---|
| `COMPLETED` | 写盘及缓存释放完成 |
| `PAUSED` | 保存中断，需排查 |
| `CANCELLED` | 已取消 |

`COMPLETED` 后查看 `missing_by_server`：值大于 0 表示该节点有缺包。查询或触发超时不等于触发失败，不能直接重复触发；先用已取得的 `event_id` 查询。命令运行信息写入标准错误，自动生成的事件编号也会在提交前输出到标准错误。若仍未取得编号，可用 `tltrigger wait --json` 查询并等待当前未完成事件；事件已完成时该方式无法找回结果。

需要指定历史起点时，可用 `tltrigger 3 --288 --period 7010000 --json`，其中 `--period` 后是绝对积分周期。

需要为不同频段分别设置范围时，使用第 5 节 TCP `CAPTURE` 报文中的 `reference_period` 和 `node_ranges`。各节点范围必须已在缓存中；完成后按节点查看各自保存范围。

## 4. 启停缓存节点

```bash
tlcache start 1-3 --json
tlcache stop 1-3 --yes --json
tlcache restart 2 --yes --json
```

编号 1～14 对应机柜标签 `tianlaistore1`～`tianlaistore14`；也支持 `all` 和 `2,5-7`。显控界面在停止或重启前应提示用户：内存中的缓存数据将丢失。`--yes` 表示界面已完成确认。

回复顶层 `status:OK` 不保证每台都成功，要逐项检查 `nodes` 中的 `status`、`state` 和 `error`。停止返回 `ACCEPTED`、`stopping` 表示已提交；继续运行 `tlcache --json` 确认节点变为 `STOPPED`。未完成事件会阻止停止或重启；只有用户明确要求丢弃事件时才增加 `--force`。

例如以下回复表示停止请求已提交，尚未完成：

```json
{"status":"OK","action":"STOP","nodes":{"0":{"status":"ACCEPTED","state":"stopping"}}}
```

## 5. 直接使用 TCP 报文

如果显控程序不调用命令行工具，可以连接控制器 `172.0.0.142:18999`。**每个 TCP 连接发送一条请求、接收一条回复，然后关闭。**请求和回复使用相同封装：

| 顺序 | 长度 | 内容 |
|---|---:|---|
| 1 | 4 字节 | 后续 JSON 的字节数，`uint32` 大端序 |
| 2 | 1～1200 字节 | UTF-8 编码的 JSON 对象 |

长度是 UTF-8 **字节数**，不是字符数。例如查询存储状态的 JSON 正文是：

```json
{"version":1,"command":"QUERY_STORAGE"}
```

发送时先写入该正文长度的 4 字节大端整数，再写入正文；接收时先读满 4 字节，根据长度读满回复 JSON。TCP 的一次 `recv` 不保证读到完整报文。

以下 Python 3 示例查询存储状态，复制后可直接运行：

```python
import json
import socket
import struct


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed before the reply was complete")
        data.extend(chunk)
    return bytes(data)


def request_controller(request):
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(body) <= 1200:
        raise ValueError("request JSON must be 1-1200 bytes")

    with socket.create_connection(("172.0.0.142", 18999), timeout=10) as sock:
        sock.sendall(struct.pack("!I", len(body)) + body)
        size = struct.unpack("!I", read_exact(sock, 4))[0]
        if not 1 <= size <= 1200:
            raise ValueError("invalid reply length")
        return json.loads(read_exact(sock, size).decode("utf-8"))


reply = request_controller({"version": 1, "command": "QUERY_STORAGE"})
print(json.dumps(reply, ensure_ascii=False, indent=2))
```

`request_controller()` 可用于本节其他请求。控制器地址以实际部署配置为准。

其他常用请求正文：

| 操作 | JSON 正文 |
|---|---|
| 查询可触发范围 | `{"version":1,"command":"QUERY_STATUS","server_ids":[0,1,2,3,4,5,6,7,8,9,10]}` |
| 查询当前事件 | `{"version":1,"command":"QUERY_ACTIVE_EVENT"}` |
| 启动前三台 | `{"version":1,"command":"CACHE_SERVICE","action":"START","server_ids":[0,1,2]}` |

控制器的状态查询不包含每个网口详情；需要该信息时使用第 1 节的 `tlcache --json`。服务启停接口仅接受授权来源，显控节点默认地址为 `172.0.0.61`。

直接触发 3 秒 288 路数据的正文：

```json
{"version":1,"command":"CAPTURE","server_ids":[0,1,2,3,4,5,6,7,8,9,10],"start":{"trace_period_id":120,"integration_period_id":45000},"duration_ms":3000,"notify":false}
```

控制器会在回复中返回 `request_id` 和 `event_id`；保存 `event_id`，随后发送 `{"version":1,"command":"QUERY_EVENT","event_id":"收到的事件编号"}` 查询结果。触发回复的 `PREPARING` 不表示写盘完成。若需要安全重试超时请求，首次请求就填写唯一的 `request_id` 和 `event_id`，并从同一来源 IP 重发完全相同的报文。字段和存储格式详见 [PROTOCOL.md](PROTOCOL.md)。

按缓存服务器指定不同时间范围：

```json
{"version":1,"command":"CAPTURE","reference_period":7010000,"node_ranges":{"0":{"offset_seconds":-2.0,"duration_seconds":0.5},"1":{"offset_seconds":-1.0,"duration_seconds":0.2}},"notify":false}
```

`reference_period` 是后端提供的触发时刻绝对积分周期。`node_ranges` 的键 `0`、`1` 对应 `tianlaistore1`、`tianlaistore2`；起点为基准周期加 `offset_seconds`，`duration_seconds` 是保存时长。结束位置不能晚于基准周期。时长为 0.1～50 秒且以 0.1 秒递增；偏移和时长须对齐积分周期。

触发请求不填写路数：只选 1～11 号服务器按 288 路模式汇总，包含 12～14 号时按 384 路模式汇总。

只触发 `tianlaistore2` 时，在 `node_ranges` 中只保留键 `"1"`：

```json
{"version":1,"command":"CAPTURE","reference_period":7010000,"node_ranges":{"1":{"offset_seconds":-2.0,"duration_seconds":0.5}},"notify":false}
```

以默认 200 微秒积分周期计算，该节点保存绝对周期 `[7000000,7002500)`。界面应在提交前用 `{"version":1,"command":"QUERY_STATUS","server_ids":[1]}` 查询该节点稳定范围，确保保存区间完整落在 `[oldest_period,settled_end)` 内；提交后用返回的 `event_id` 查询完成状态。编号规则对所有节点相同：`tianlaistoreN` 使用内部编号 `N-1`。

单台节点运行状态及服务启停也可直接使用：

```bash
tlcache status 2 --json
tlcache start 2 --json
tlcache stop 2 --yes --json
```

这些命令里的 `2` 使用机柜标签编号，而 TCP JSON 使用内部编号 `1`。`tlcache status 2 --json` 仍包含存储服务状态，`cache_nodes` 中只有所选节点。


## 6. JSON 字段速查

以下列出本文涉及接口的字段。示例为便于阅读只展示部分字段；异常、节点离线、事件阶段不同或不同版本更新期间，部分字段可能缺失。显控读取字段时应允许缺失。时间字段以名称中的 `ms`（毫秒）、`seconds`（秒）或 `bytes`（字节）为单位。

### 6.1 公共字段

| 字段 | 含义 |
|---|---|
| `version` | 协议版本，当前为 1 |
| `request_id` | 请求编号；控制器请求可省略，回复返回生成的编号 |
| `command` | 请求操作名称 |
| `status` | 本次请求或事件状态；`ERROR` 表示出错 |
| `error` | 错误说明，仅部分错误或阶段返回 |
| `server_id` | 内部节点编号 0～13；界面显示为 `tianlaistore` 加编号 1～14 |

### 6.2 `tlcache --json` 中的存储状态

`tlcache --json` 返回顶层 `storage` 和 `cache_nodes`；`tlcache storage --json` 直接返回存储对象。控制器 `QUERY_STORAGE` 回复包含公共字段和 `storage` 对象。

| `storage` 字段 | 含义 |
|---|---|
| `version`、`service`、`pid` | 状态格式版本、服务名、进程号 |
| `state`、`state_age_ms` | 存储运行状态、该状态持续毫秒数 |
| `updated_unix` | 状态更新时间，Unix 秒 |
| `listeners_ready`、`listener_count` | 已就绪 / 配置的监听数 |
| `active_transfers` | 正在传输的通道数 |
| `event_id` | 当前或最近一次事件编号 |
| `event_expected_lanes`、`event_completed_lanes`、`event_aborted_lanes` | 本事件预期、完成及中断的通道数 |
| `event_expected_bytes`、`event_written_bytes` | 本事件预计及累计写入字节数 |
| `write_rate_bytes_per_second` | 最近采样周期的写入速度 |
| `completed_lanes_total`、`aborted_lanes_total`、`written_bytes_total` | 存储进程启动以来的累计完成通道、中断通道和写入字节数 |
| `notification_state`、`notification_error` | 后端通知状态和错误 |
| `last_error` | 最近一次存储错误 |
| `error` | 状态查询失败原因，离线时可能出现 |

### 6.3 `cache_nodes[]` 节点状态

| 字段 | 含义 |
|---|---|
| `version`、`request_id`、`server_id`、`status` | 节点协议、请求编号、节点编号及查询状态 |
| `host`、`service_state` | 节点管理地址、systemd 服务状态；后者可能缺失 |
| `runtime_state`、`runtime_state_age_ms`、`uptime_ms` | 程序运行阶段、阶段持续时间、进程运行时间 |
| `accept_trigger` | 节点是否接受触发 |
| `oldest_period`、`settled_end` | 本节点稳定周期范围，结束值不包含 |
| `event_id`、`event_state` | 当前或最近事件编号及节点事件状态 |
| `initialized_ports`、`receive_ports`、`receiving_ports` | 已初始化、配置、近期有数据的接收口数 |
| `data_flow`、`last_packet_age_ms`、`inactive_ports` | 收数状态、最近收包间隔、无数据口列表 |
| `received_packets`、`lost_packets` | 本次进程运行以来接收和缺失的包数 |
| `loss_periods`、`last_loss_period`、`lost_by_port` | 缺包影响周期数、最近缺包周期、各口累计缺包 |
| `late_packets`、`ignored_packets` | 迟到包数、忽略包数 |
| `transition_used_periods`、`transition_capacity_periods`、`transition_warning` | 过渡区使用周期数、容量及告警 |
| `last_freeze_ms` | 最近一次冻结操作耗时 |
| `error` | 查询失败或节点错误时的说明 |

### 6.4 `tltrigger status --json` 和 `QUERY_STATUS`

请求包括公共字段及可选 `server_ids` 内部节点编号列表；省略时查询所有启用节点。

| 回复字段 | 含义 |
|---|---|
| `status`、`accept_trigger` | 汇总状态；`READY` 且 `true` 时可触发 |
| `channel_count` | 使用的通道数 |
| `oldest_period`、`settled_end` | 参与节点共同稳定范围，结束值不包含 |
| `partial_node_set`、`participating_server_ids` | 是否为不完整节点集合、实际参与节点编号 |
| `node_runtime_states`、`runtime_state_counts` | 各节点运行状态、各状态节点数 |

### 6.5 触发和事件查询

直接 `CAPTURE` 请求除公共字段外包含：

| 字段 | 要求和含义 |
|---|---|
| `event_id` | 可选；省略时控制器生成；安全重试时应由调用方提供唯一编号 |
| `server_ids` | 可选的内部节点编号列表；省略时选择所有启用节点，逐节点模式默认采用 `node_ranges` 的键 |
| `start.trace_period_id`、`start.integration_period_id` | 必填，起始 trace 和 trace 内积分周期 |
| `duration_ms`、`period_count` | 二选一，时长 0.1～50 秒，步长 0.1 秒，且须为整数个积分周期 |
| `notify` | 可选布尔值；`false` 用查询等待；直接 TCP 请求默认主动通知触发连接来源 IP 的 19001 端口 |
| `reference_period`、`node_ranges` | 可选的逐节点模式：基准绝对周期及节点参数对象；与统一范围字段互斥 |
| `node_ranges[编号].offset_seconds`、`duration_seconds` | 相对基准周期的起点偏移和保存时长，单位秒 |

`QUERY_EVENT` 请求必须填写 `event_id`；`QUERY_ACTIVE_EVENT` 不需要事件编号。事件回复可能包含：

| 字段 | 含义 |
|---|---|
| `event_id`、`acquisition_id`、`status` | 事件编号、内部采集标识、事件阶段 |
| `start`、`end_exclusive` | 起止位置，均包含 `trace_period_id` 和 `integration_period_id`；结束位置不包含 |
| `period_count`、`period_microseconds`、`integration_per_trace` | 周期数、单周期微秒数、每 trace 周期数 |
| `channel_count`、`partial_node_set`、`participating_server_ids` | 路数、节点集合是否不完整、参与节点 |
| `active_server_ids` | 当前事件活跃节点编号 |
| `missing_by_server` | 各节点缺包数；键为内部编号，准备后可能出现 |
| `result_address`、`result_port` | 完成通知目标；不通知时分别为空字符串和 0 |
| `error` | 暂停等阶段的错误信息，可能缺失或为空 |
| `reference_period`、`node_ranges`、`node_ranges_omitted` | 逐节点模式的基准周期、各节点 `[起始绝对周期, 周期数]`；超出报文上限时省略范围并标记 |

事件状态依次可能为 `PREPARING`、`PREPARED`、`STORING`、`STORED`、`RELEASING`、`COMPLETED`；异常时可能为 `PAUSED` 或 `CANCELLED`。

### 6.6 `CACHE_SERVICE` 节点启停

请求包含 `action`（`START`、`STOP`、`RESTART`、`STATUS`）、非空且不重复的 `server_ids` 列表，以及可选布尔值 `force`（默认 `false`）。

| 回复字段 | 含义 |
|---|---|
| `action`、`nodes` | 本次操作、按内部编号分组的结果 |
| `nodes[编号].status`、`state`、`error` | 单节点执行结果、服务状态、可选错误 |
| `errors_omitted` | 为满足报文大小限制省略了部分错误详情时为 `true` |
