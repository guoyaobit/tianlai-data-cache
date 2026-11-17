# Trigger and Stored Data Interface

本文面向消色散后端和数据读取方，介绍如何触发缓存、确认写盘，以及如何解析最终文件。日常命令用法见 [Dedispersion Backend Trigger Guide](DEDISPERSION_BACKEND_GUIDE.md)。

## 1. 如何触发

### 1.1 使用命令

在已安装 `tltrigger`、能访问管理网的节点执行：

```bash
tltrigger                   # 查看384路是否可以触发
tltrigger period --288      # 查看288路共同可用积分周期
tltrigger 3 --288           # 保存最近3秒的288路数据，等待完成
tltrigger 3                 # 保存最近3秒的384路数据，等待完成
tltrigger 3 --period 7010000  # 从指定绝对积分周期开始，保存3秒
```

数字命令的单位是**秒**，支持 1～50 秒；省略 `--288` 时使用384路。命令会自动生成事件编号并选择对应服务器。更多查询和等待命令见[使用指南](DEDISPERSION_BACKEND_GUIDE.md)。

### 1.2 后端直接发送触发

后端通过管理网向控制器发送 TCP + JSON：

| 项目 | 默认值 |
|---|---|
| 控制器地址 | `172.0.0.142:18999`，以实际部署配置为准 |
| 消息格式 | 4字节无符号大端长度 + UTF-8 JSON |
| 单条 JSON 长度 | 1～1200 字节，长度按编码后的字节计算 |
| 连接方式 | 每条连接发送一个请求、接收一个回复，随后关闭 |

例如，从指定积分周期开始保存 3 秒的 288 路数据：

```json
{
  "version": 1,
  "request_id": "req-000001",
  "command": "CAPTURE",
  "event_id": "event-000001",
  "channel_count": 288,
  "start": {
    "trace_period_id": 120,
    "integration_period_id": 45000
  },
  "duration_ms": 3000
}
```

| 字段 | 要求 | 说明 |
|---|---|---|
| `version` | 必填，`1` | 协议版本 |
| `request_id` | 必填 | 请求与回复匹配编号 |
| `command` | 必填，`CAPTURE` | 发起保存 |
| `event_id` | 必填 | 全局唯一的事件编号，用于查询和去重 |
| `channel_count` | 可选，`288` 或 `384` | 默认 `384`；自动选择对应服务器 |
| `start` | 必填 | 起始 trace 与积分周期编号 |
| `duration_ms` | 与 `period_count` 二选一 | 保存时长，正整数毫秒，须为整数个周期 |
| `period_count` | 与 `duration_ms` 二选一 | 保存的正整数积分周期数 |

`request_id` 和 `event_id` 使用 1～64 个 ASCII 字母、数字、下划线或短横线。默认每周期 **200 微秒**，每个 trace 为 **50000 周期**；实际数值以系统配置为准，控制器与缓存节点必须一致。单次最长 50 秒。上例的 3000 毫秒等于 15000 个周期，保存区间为 `[(120,45000), (121,10000))`，不含结束位置。

直接接口只接受已稳定且仍在所有参与节点共同缓存范围内的数据，不等待未来的数据；后端可先发送 `QUERY_STATUS` 获取 `oldest_period` 与 `settled_end`，其中后者是排他边界。绝对积分周期计算为 `trace_period_id × 50000 + integration_period_id`（50000 为默认配置）。各数据源须使用同一个积分计数起点，系统不会自动校正源间偏移。

直接 TCP 调用示例：

```python
import json
import socket
import struct

request = {
    "version": 1, "request_id": "req-000001", "command": "CAPTURE",
    "event_id": "event-000001", "channel_count": 288,
    "start": {"trace_period_id": 120, "integration_period_id": 45000},
    "duration_ms": 3000,
}

def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("incomplete reply")
        data.extend(chunk)
    return bytes(data)

body = json.dumps(request, separators=(",", ":")).encode("utf-8")
with socket.create_connection(("172.0.0.142", 18999), timeout=10) as sock:
    sock.sendall(struct.pack("!I", len(body)) + body)
    size = struct.unpack("!I", read_exact(sock, 4))[0]
    if not 1 <= size <= 1200:
        raise ValueError("invalid reply length")
    reply = json.loads(read_exact(sock, size))
    if reply.get("request_id") != request["request_id"]:
        raise ValueError("request_id mismatch")
    print(reply)
```

每次新事件必须使用新的 `event_id`；每条不同的请求必须使用新的 `request_id`。连接超时不代表触发失败：先按事件编号查询，或从相同来源 IP 使用相同 `request_id` 和完全相同的报文重试，避免重复触发。

### 1.3 查询和完成通知

初次返回 `PREPARING` 表示控制器已经接受事件，**不表示写盘完成**。使用相同的 TCP 封装发送：

```json
{"version":1,"request_id":"req-000002","command":"QUERY_EVENT","event_id":"event-000001"}
```

查询结果中的 `COMPLETED` 表示所选节点数据已持久化，缓存也已释放；`PAUSED` 表示需要排查，不表示成功。确认事件完成后，再根据缺包统计判断数据是否完整。

### Completion Notification

直接发送 `CAPTURE` 时，存储服务器写盘完成后主动连接**该触发 TCP 连接的来源 IP、端口 19001**，发送同样的长度前缀 JSON。后端应在触发前监听该端口；请求中不填写 `result_address` 或 `result_port`。以下通知为字段摘录，实际消息还包含地址、时间参数和参与节点等字段：

```json
{
  "version": 1,
  "command": "CAPTURE_RESULT",
  "status": "STORED",
  "event_id": "event-000001",
  "channel_count": 288,
  "period_count": 15000,
  "missing_by_server": {"0": 0, "1": 2},
  "partial_node_set": false
}
```

`STORED` 表示本次选定节点的数据已持久化，不保证零缺包。`missing_by_server` 的键为内部节点编号，值为该节点缺失的包位置总数；`partial_node_set=true` 表示只有部分节点参与，不能视为完整288路或384路数据。

后端持久化并按 `event_id` 幂等处理结果后，在**同一连接**回复长度前缀 JSON：

```json
{"version":1,"command":"ACK_RESULT","status":"ACKNOWLEDGED","event_id":"event-000001"}
```

未收到确认时存储服务器会重试，后端需能处理重复通知。`tltrigger` 默认通过轮询等待，不要求监听通知，也不主动请求回调；`COMPLETED` 不代表后端已确认通知。

## 2. 最终存储文件格式

### Stored Files

默认根目录为存储服务器的 `/data`，实际根目录由 `storage.yaml` 的 `directory` 指定。自动生成的事件编号形如 `YYYYMMDD-HHMMSS-六位十六进制`，目录使用前面的 `YYYYMMDD-HHMMSS`，其中 `tltrigger` 生成的时间为 UTC；其他合法事件编号直接用作目录名。同一秒的自动事件可能对应同一目录，发生元数据冲突时会拒绝覆盖。

```text
/data/事件目录/
├── event.json
├── p00-07/
│   ├── lane-0.json
│   ├── lane-1.json
│   └── packet-00_boards-00-35.bin
├── p24-25/
│   └── packet-24_boards-00-17.bin
└── f0688-1023/
    └── frequency-0688-0773_boards-36-47.bin
```

上图只列出部分文件。每个接收口生成一个 `.bin`：

| 内部节点 | 数据目录 | 文件名含义 |
|---|---|---|
| 0～2 | `p00-07`、`p08-15`、`p16-23` | `packet-包号_boards-00-35.bin` |
| 3～10 | `p24-25` 至 `p38-39` | `packet-包号_boards-00-17.bin` 或 `boards-18-35.bin` |
| 11～13 | `f0000-0343`、`f0344-0687`、`f0688-1023` | `frequency-起始频点-结束频点_boards-36-47.bin` |

### 2.1 二进制记录

每个 `.bin` 是多个固定长度的积分记录顺序拼接，**没有文件头、Ethernet/IP/UDP 头或40字节传输块头**。所有整数为大端：

| 记录偏移 | 大小 | 字段 | 含义 |
|---:|---:|---|---|
| 0 | 8 B | `absolutePeriod` | 绝对积分周期 |
| 8 | 8 B | `boardMask` | 第 `n` 位表示第 `n` 个包位置是否收到 |
| 16 | 4 B | `packetBytes` | 一个包位置的字节数 |
| 20 | 4 B | `packetSlotCount` | 每周期的包位置数 |
| 24 | `packetBytes × packetSlotCount` | 原始包 | 按位置顺序排列，每包含16字节原始业务头及数据 |

位图为 1 表示该位置收到数据；位图为 0 时，整个包位置填零。整周期缺失也保留一条记录，位图和包位置均为零。数据保持原始格式，不转换为浮点数。

| 内部节点 | 每周期位置数 | 每包字节数 | 每条记录字节数 |
|---|---:|---:|---:|
| 0～2 | 36 | 2320 | 83544 |
| 3～10 | 18 | 8848 | 159288 |
| 11～13 | 24 | 8272 | 198552 |

文件大小为 `period_count × 每条记录字节数`。按默认200微秒周期，3秒为15000条记录。第 `i` 条记录的偏移是 `i × 每条记录字节数`，周期编号可通过记录头确认。

每个原始包的16字节业务头中：字节0低4位与字节1～2组成 `trace_period_id`；字节3低7位与字节4～5组成 `integration_period_id`；字节6为板卡号，字节7为包号；字节3最高位为 `ifTrace`。字节0高4位不参与接收校验，原始头其余字节保留。

远端节点11～13每块板卡每周期有两个包，槽位按板卡排列：`slot = (boardId - 36) × 2 + (packetId - 2g)`，其中 `g = (server_id - 11) × 4 + port`。最后一个频率文件名为 `frequency-0946-1023_boards-36-47.bin`，末尾8个填充频点仍保留在包中。

### 2.2 判断文件是否写完

- 每个数据目录下的 `lane-0.json` 和 `lane-1.json` 记录通道状态；`status=STORED` 时，`missing_packets` 按接收口统计缺包。
- 事件目录的 `event.json` 在本次选定节点的两条通道全部持久化后生成，包含事件结果和 `missing_by_server`。`backend_acknowledged` 表示通知确认状态；如果该事件未启用通知，值为 `true` 并不表示后端收到通知。
- 数据文件在接收开始时就会被设置为最终大小，因此**不能仅凭文件存在或大小判断完成**。应检查事件结果及缺包数。

目录权限为 `0755`，数据及 JSON 文件权限为 `0644`。文件不包含应用层 CRC；接收侧或存储介质的静默损坏不由该格式检测。
