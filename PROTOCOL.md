# Trigger and Stored Data Interface

本文面向消色散后端和数据读取方，可独立使用，介绍如何触发缓存、确认写盘，以及如何解析最终文件。

## 1. 如何触发

### 1.1 使用命令

#### 单独安装 `tltrigger`

将交付的 **`trigger.py`** 放到客户服务器上。服务器需安装 Python 3.10 或更高版本，并能访问控制器 `172.0.0.142:18999`。脚本只依赖 Python 标准库，无需安装其他 Python 包。

在 `trigger.py` 所在目录，可直接运行：

```bash
python3 trigger.py              # 查看状态
python3 trigger.py 3 --288      # 保存最近3秒的288路数据
```

如需在任意目录直接使用 `tltrigger` 命令，执行一次：

```bash
sudo install -m 0755 trigger.py /usr/local/bin/tltrigger
tltrigger
```

安装需要 `sudo`，并会替换已有同名命令。更新时，用新版本 `trigger.py` 重新执行安装命令即可，无需重启服务。控制器地址与默认值不同时，在命令前指定，例如 `tltrigger --host 控制器IP --port 18999 status`。

#### 触发和查询

在已安装 `tltrigger` 且能访问控制器的节点执行：

```bash
tltrigger                      # 查看384路是否可以触发
tltrigger period --288         # 查看288路共同可用积分周期
tltrigger 3 --288              # 保存最近3秒的288路数据，并等待完成
tltrigger 3                    # 保存最近3秒的384路数据，并等待完成
tltrigger 3 --period 7010000   # 从指定绝对积分周期开始，保存3秒
tltrigger wait                 # 等待当前未完成事件
```

数字命令的单位是秒，支持 0.1～50 秒，步长 0.1 秒，例如 `tltrigger 0.1 --288`；不指定 `--288` 时默认384路。命令自动生成事件编号并选择对应节点。不指定 `--period` 时，自动保存共同缓存中最新、已稳定的数据。

也可以将以下 JSON 保存为 `trigger.json`，运行 `tltrigger trigger.json`：

```json
{"seconds":3,"period":7010000,"channels":288}
```

`seconds` 必填；`period` 和 `channels` 可省略。省略 `period` 时选择最新稳定数据，省略 `channels` 时默认384路。数字命令和简化 JSON 命令默认等待完成；如需只提交不等待，可使用 `tltrigger capture 3 --288`，再用 `tltrigger wait` 等待。

执行前应确认状态为 `READY`。输出 `COMPLETED` 表示保存完成并已释放缓存；`PAUSED` 表示需要排查，不表示成功。命令超时或中断时，可运行 `tltrigger wait` 或 `tltrigger EVENT_ID` 查询已有事件，避免重复触发。完成后再次触发前，仍需等待状态重新变为 `READY`。

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
  "command": "CAPTURE",
  "server_ids": [0,1,2,3,4,5,6,7,8,9,10],
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
| `command` | 必填，`CAPTURE` | 发起保存 |
| `server_ids` | 可选 | 内部服务器编号列表；省略时选择所有启用节点 |
| `start` | 必填 | 起始 trace 与积分周期编号 |
| `duration_ms` | 与 `period_count` 二选一 | 100～50000 毫秒，100 毫秒的整数倍，且须为整数个积分周期 |
| `period_count` | 与 `duration_ms` 二选一 | 正整数积分周期数，对应时长须为 0.1～50 秒、0.1 秒的整数倍 |

触发请求无需填写 `request_id` 或 `event_id`；控制器会生成并在回复中返回。该简化格式需要控制器更新到支持自动生成编号的版本。默认每周期 **200 微秒**，每个 trace 为 **50000 周期**；实际数值以系统配置为准，控制器与缓存节点必须一致。单次最长 50 秒。上例的 3000 毫秒等于 15000 个周期，保存区间为 `[(120,45000), (121,10000))`，不含结束位置。

直接接口只接受已稳定且仍在所有参与节点共同缓存范围内的数据，不等待未来的数据；后端可先发送 `QUERY_STATUS` 获取 `oldest_period` 与 `settled_end`，其中后者是排他边界。绝对积分周期计算为 `trace_period_id × 50000 + integration_period_id`（50000 为默认配置）。各数据源须使用同一个积分计数起点，系统不会自动校正源间偏移。

查询288路缓存状态的请求如下，同样使用上面的 TCP 报文封装；仅当回复的 `status` 为 `READY` 且 `accept_trigger` 为 `true` 时才提交触发：

```json
{"version":1,"command":"QUERY_STATUS","server_ids":[0,1,2,3,4,5,6,7,8,9,10]}
```

直接 TCP 调用示例：

```python
import json
import socket
import struct

request = {
    "version": 1, "command": "CAPTURE", "server_ids": list(range(11)),
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
    print(reply)
```

连接超时不代表触发失败，不要直接重复发送触发。可先查询当前活动事件，但事件如果已经完成而回复中的事件编号也未收到，就无法通过活动事件查询可靠找回结果。如果需要安全重试同一请求，调用方应自行提供唯一的 `request_id` 和 `event_id`，并从同一来源 IP 使用相同编号及完全相同的报文重试。编号使用1～64位 ASCII 字母、数字、下划线或短横线，不同事件不得复用事件编号；控制器的请求重放缓存保留约5分钟。

### 按缓存服务器设置时间范围

后端提供触发时刻对应的绝对积分周期 `reference_period`。每个服务器单独设置相对该周期的起点偏移 `offset_seconds` 和保存时长 `duration_seconds`，均使用秒，可以为小数。对象键为内部编号：`0` 对应 `tianlaistore1`，`1` 对应 `tianlaistore2`。

使用此功能前，需要更新控制器、存储程序和 `trigger.py`。旧版不支持逐服务器时间范围；更新前产生的活动事件应先处理完毕。

```json
{
  "version": 1,
  "command": "CAPTURE",
  "reference_period": 7010000,
  "node_ranges": {
    "0": {"offset_seconds": -2.0, "duration_seconds": 0.5},
    "1": {"offset_seconds": -1.0, "duration_seconds": 0.2}
  },
  "notify": false
}
```

示例只触发两台服务器：第一台保存触发前 2.0～1.5 秒，第二台保存触发前 1.0～0.8 秒。省略 `server_ids` 时，从 `node_ranges` 的键选择服务器；若填写，必须与键完全一致。不能与统一范围的 `start`、`duration_ms` 或 `period_count` 混用。

触发请求无需填写路数：只选择内部编号 0～10 时按 288 路模式汇总，包含 11～13 时按 384 路模式汇总。完成结果中的 `channel_count` 和 `partial_node_set` 反映该自动选择的模式。只选择前 11 台时，结果按 288 路模式判断完整性。

时长为 0.1～50 秒、步长 0.1 秒；偏移和时长均须对齐积分周期，结束位置不能晚于基准周期。所有区间必须仍在各自节点已稳定的缓存内。控制器接收后异步检查，失败时事件为 `PAUSED`，需查看 `error`；不要将 `PREPARING` 视为已完成。

事件回复中的 `node_ranges` 为各节点的 `[起始绝对周期, 周期数]`。如回复超出报文长度上限，控制器会省略它并标记 `node_ranges_omitted: true`。存储完成时，不同节点范围会在事件结果的 `server_ranges` 中以同样格式记录；如所有节点范围一致，仍使用原有的 `start`、`end_exclusive` 和 `period_count` 字段。

也可将上述完整 JSON 保存为文件，执行 `python3 trigger.py 请求.json`；该方式默认关闭主动通知并等待事件结束。直接 TCP 请求如需主动通知，可省略 `notify` 或设为 `true`，后端须监听触发来源地址的 19001 端口。

### 单独触发一台缓存服务器

以下保存 `tianlaistore2` 对应频段的数据：以触发基准周期为参照，从触发前 2.0 秒开始保存 0.5 秒。JSON 的键 `"1"` 是内部编号，等于服务器标签编号减 1。

```json
{
  "version": 1,
  "command": "CAPTURE",
  "reference_period": 7010000,
  "node_ranges": {
    "1": {"offset_seconds": -2.0, "duration_seconds": 0.5}
  },
  "notify": false
}
```

将内容保存为 `store2.json`，在 `trigger.py` 所在目录执行：

```bash
python3 trigger.py store2.json
```

该命令提交后等待完成。也可通过 TCP 直接发送相同 JSON 并用返回的事件编号查询状态。要触发其他服务器，将键改为对应内部编号即可；一次请求可列出多台服务器，每台使用自己的偏移和时长。本示例只涉及一台服务器，不代表整个观测频段已经保存。

提交前可通过控制器 TCP 查询该节点的稳定范围：

```json
{"version":1,"command":"QUERY_STATUS","server_ids":[1]}
```

`oldest_period` 包含在范围内，`settled_end` 不包含在范围内。以默认 200 微秒积分周期为例，上述触发区间是 `[7000000,7002500)`；完整区间必须处于该节点的稳定范围。

### 1.3 查询和完成通知

初次返回 `PREPARING` 表示控制器已经接受事件，**不表示写盘完成**。使用相同的 TCP 封装发送：

```json
{"version":1,"command":"QUERY_ACTIVE_EVENT"}
```

保存初次回复中的 `event_id` 后，也可用 `QUERY_EVENT` 和该编号查询，包括已完成事件。`QUERY_ACTIVE_EVENT` 仅查询未完成事件，若事件已经完成或不存在则返回 `NO_ACTIVE_EVENT`。查询结果中的 `COMPLETED` 表示所选节点数据已持久化，缓存也已释放；`PAUSED` 表示需要排查，不表示成功。确认事件完成后，再根据缺包统计判断数据是否完整。

### Completion Notification

直接发送 `CAPTURE` 时，存储服务器写盘完成后主动连接**该触发 TCP 连接的来源 IP、端口 19001**，发送同样的长度前缀 JSON。后端应在触发前监听该端口；请求中不填写 `result_address` 或 `result_port`。以下通知为字段摘录，实际消息还包含地址、时间参数和参与节点等字段：

存储服务器的管理网地址固定为 `172.0.0.142`。当前配置由系统路由选择完成通知的源地址；如后端按来源地址过滤，应确认实际源地址。

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

表中的节点是程序内部编号，与机柜上的服务器标签相差 1：内部节点 0 对应 `tianlaistore1`，节点 13 对应 `tianlaistore14`。例如内部节点 3 的四个接收口分别生成：

```text
p24-25/packet-24_boards-00-17.bin   # 口0
p24-25/packet-24_boards-18-35.bin   # 口1
p24-25/packet-25_boards-00-17.bin   # 口2
p24-25/packet-25_boards-18-35.bin   # 口3
```

`lane-0.json` 记录本节点偶数口，`lane-1.json` 记录奇数口；它们是状态文件，不是数据文件。

### 2.1 二进制记录

每个 `.bin` 是多个固定长度的积分记录顺序拼接，**没有文件头、Ethernet/IP/UDP 头或40字节传输块头**。一个文件只对应一个接收口，包含本次事件指定的所有积分周期，排列如下：

| 文件内顺序 | 文件偏移 | 内容 |
|---:|---:|---|
| 第 0 条 | `0` | 起始积分周期的完整记录 |
| 第 1 条 | `记录长度` | 起始积分周期 + 1 的完整记录 |
| 第 `i` 条 | `i × 记录长度` | 起始积分周期 + `i` 的完整记录 |
| 最后一条 | `(period_count - 1) × 记录长度` | 本次保存区间的最后一个积分周期 |

每条积分记录先是24字节记录头，再按包位置顺序存放数据；所有记录头整数为**大端**：

| 记录偏移 | 大小 | 字段 | 含义 |
|---:|---:|---|---|
| 0 | 8 B | `absolutePeriod` | 绝对积分周期 |
| 8 | 8 B | `boardMask` | 第 `n` 位表示第 `n` 个包位置是否收到 |
| 16 | 4 B | `packetBytes` | 一个包位置的字节数 |
| 20 | 4 B | `packetSlotCount` | 每周期的包位置数 |
| 24 | `packetBytes × packetSlotCount` | 包位置0、1、… | 依次排列，每个包包含16字节原始业务头及数据区 |

| 记录内顺序 | 记录内偏移 | 内容 |
|---:|---:|---|
| 记录头 | `0` | 周期、位图、单包长度和包位置数，共24字节 |
| 包位置0 | `24` | 第1个原始包 |
| 包位置1 | `24 + packetBytes` | 第2个原始包 |
| 包位置 `n` | `24 + n × packetBytes` | 第 `n + 1` 个原始包 |
| 最后位置 | `24 + (packetSlotCount - 1) × packetBytes` | 本积分周期的最后一个原始包 |

位图为 1 表示该位置收到数据；位图为 0 时，整个包位置填零。整周期缺失也保留一条记录，位图和包位置均为零。数据保持原始格式，不转换为浮点数。

位图从最低位开始对应包位置：例如 `boardMask = 0x05` 表示位置 0 和 2 已收到、位置 1 未收到。不要根据数据是否全零判断缺包，应以位图为准。三类完整记录的位图分别为 `0xfffffffff`（36位）、`0x3ffff`（18位）、`0xffffff`（24位）。

| 内部节点 | 每周期位置数 | 每包字节数 | 每条记录字节数 |
|---|---:|---:|---:|
| 0～2 | 36 | 2320 | 83544 |
| 3～10 | 18 | 8848 | 159288 |
| 11～13 | 24 | 8272 | 198552 |

文件大小为 `period_count × 每条记录字节数`。按默认200微秒周期，3秒为15000条记录：

| 内部节点 | 3秒时单个 `.bin` 的大小（字节） |
|---|---:|
| 0～2 | 1,253,160,000 |
| 3～10 | 2,389,320,000 |
| 11～13 | 2,978,280,000 |

第 `i` 条记录（从 0 开始）的文件偏移为 `i × 每条记录字节数`，其绝对积分周期从事件起点依次递增。记录内第 `n` 个包的偏移为 `24 + n × packetBytes`，先读取该记录的位图，再按偏移读取包。

每个原始包的位置内先保存16字节业务头，再保存业务数据。偏移相对于该包位置的起点：

| 包内偏移 | 大小 | 内容 |
|---:|---:|---|
| `0` | 16 B | FPGA原始业务头，原样保存 |
| `16` | `packetBytes - 16` B | FPGA业务数据区，原样保存 |

不同节点类型的数据区长度及结构如下：

| 内部节点 | 原始包总长 | 业务头 | 数据区 | 数据区内容 |
|---|---:|---:|---:|---|
| 0～2 | 2320 B | 16 B | 2304 B | 单板：24个FFT、12个频点、8个通道，共2304个1 B样本 |
| 3～10 | 8848 B | 16 B | 8832 B | 单板：24个FFT、46个频点、8个通道，共8832个1 B样本 |
| 11～13 | 8272 B | 16 B | 8256 B | `[12 FFT][86频点][8通道]`，每个复数样本1 B |

上表中原系统节点0～10的FFT、频点、通道为数据维度，并不表示维度的存储先后顺序。接收和存储时未对数据区解包或重排，内部字节顺序需按 FPGA 采集端的数据格式解析；不能仅依据文件记录头推断。

16字节业务头的字段如下，偏移相对于原始包的起点：

| 业务头偏移 | 字段 | 解析方法 |
|---:|---|---|
| 0～2 | `trace_period_id` | `(byte[0] & 0x0f) << 16 \| byte[1] << 8 \| byte[2]` |
| 3 | `ifTrace` | `byte[3] >> 7` |
| 3～5 | `integration_period_id` | `(byte[3] & 0x7f) << 16 \| byte[4] << 8 \| byte[5]` |
| 6 | `boardId` | 板卡号 |
| 7 | `packetId` | 包号 |
| 8～15 | 原始字段 | 原样保留 |

字节0高4位不参与接收校验，原始业务头与数据区均按收到的内容保存。

### 2.2 板卡与包位置

对每条记录，包位置从 0 开始，顺序固定：

| 内部节点 | 位置 `n` 对应的包 |
|---|---|
| 0～2 | 板卡 `boardId = n`，共36块；同一文件的 `packetId` 相同，见文件名 |
| 3～10 | 板卡 `boardId = 文件名中的起始板卡号 + n`，共18块；同一文件的 `packetId` 相同 |
| 11～13 | 板卡 `boardId = 36 + n // 2`，每板两个位置；偶数位置是该口的偶数包，奇数位置是紧随其后的奇数包 |

例如 `packet-24_boards-18-35.bin` 中，位置0为板卡18，位置17为板卡35；位图的第0位和第17位分别指示这两个位置。

对于 `packet-24_boards-00-17.bin`，如果只有板卡1缺包，位图为 `0x3fffd`；缺失的第1个包位置从本条记录的第 `24 + 1 × 8848 = 8872` 字节开始，连续8848字节均为零。

远端节点11～13的完整公式为 `slot = (boardId - 36) × 2 + (packetId - 2g)`，其中 `g = (server_id - 11) × 4 + port`。偶数包对应 FFT 0～11，奇数包对应 FFT 12～23；每包数据区按 `[12 FFT][86频点][8通道]` 排列，每个复数样本占1字节（4位+4位）。最后一个频率文件名为 `frequency-0946-1023_boards-36-47.bin`，末尾8个填充频点仍原样保留在包中。

### 2.3 判断文件是否写完

- 每个数据目录下的 `lane-0.json` 和 `lane-1.json` 记录通道状态；`status=STORED` 时，`missing_packets` 按接收口统计缺包。
- 事件目录的 `event.json` 在本次选定节点的两条通道全部持久化后生成，包含事件结果和 `missing_by_server`。`backend_acknowledged` 表示通知确认状态；如果该事件未启用通知，值为 `true` 并不表示后端收到通知。
- 数据文件在接收开始时就会被设置为最终大小，因此**不能仅凭文件存在或大小判断完成**。应检查事件结果及缺包数。

目录权限为 `0755`，数据及 JSON 文件权限为 `0644`。文件不包含应用层 CRC；接收侧或存储介质的静默损坏不由该格式检测。

以下是状态文件中常用字段的**示意摘录**；实际文件还包含其他元数据：

`p24-25/lane-0.json`：

```json
{
  "status": "STORED",
  "event_id": "event-000001",
  "server_id": 3,
  "lane": 0,
  "ports": [0, 2],
  "start_period": 6045000,
  "period_count": 15000,
  "packet_bytes": 8848,
  "missing_packets": {"0": 0, "2": 1}
}
```

`missing_packets` 的键是本节点接收口号，不是板卡号；上例表示口2在整个事件中缺少1个包位置。`lane-N.json` 处于 `RECEIVING` 时不包含最终缺包统计。

`event.json`：

```json
{
  "status": "STORED",
  "event_id": "event-000001",
  "channel_count": 288,
  "period_count": 15000,
  "missing_by_server": {"0": 0, "1": 0, "3": 1},
  "partial_node_set": false,
  "backend_acknowledged": false
}
```

`missing_by_server` 汇总每台节点两条通道的缺包数。上例只展示部分节点；实际应结合 `participating_server_ids` 判断哪些节点参与了事件。

### 2.4 读取示例

下面的 Python 3 示例读取指定文件中的一条积分记录，打印位图和已收到包的原始业务头。无需读取整个大文件；`record_index` 从 0 开始。

```python
import os
import struct

path = "/data/事件目录/p24-25/packet-24_boards-00-17.bin"
record_index = 0
header = struct.Struct(">QQII")  # period, mask, packet_bytes, slots

with open(path, "rb") as stream:
    first = stream.read(header.size)
    if len(first) != header.size:
        raise ValueError("文件没有完整记录头")
    _, _, packet_bytes, slots = header.unpack(first)
    if (packet_bytes, slots) not in {(2320, 36), (8848, 18), (8272, 24)}:
        raise ValueError("不支持的记录格式")
    stride = header.size + packet_bytes * slots
    size = os.fstat(stream.fileno()).st_size
    if record_index < 0 or size % stride or record_index >= size // stride:
        raise ValueError("记录序号或文件长度无效")

    stream.seek(record_index * stride)
    record = stream.read(stride)
    if len(record) != stride:
        raise ValueError("记录不完整")
    period, mask, packet_bytes_check, slots_check = header.unpack_from(record)
    if (packet_bytes_check, slots_check) != (packet_bytes, slots) or mask >> slots:
        raise ValueError("记录头无效")
    print(f"period={period} mask=0x{mask:x} missing={slots - mask.bit_count()}")

    for slot in range(slots):
        if not mask & (1 << slot):
            print(f"slot={slot}: missing")
            continue
        offset = header.size + slot * packet_bytes
        raw = record[offset:offset + packet_bytes]
        trace = ((raw[0] & 0x0f) << 16) | (raw[1] << 8) | raw[2]
        integration = ((raw[3] & 0x7f) << 16) | (raw[4] << 8) | raw[5]
        print(f"slot={slot} board={raw[6]} packet={raw[7]} "
              f"trace={trace} integration={integration} payload_bytes={len(raw) - 16}")
        payload = raw[16:]  # 原始数据；如何解码由采集端数据定义决定
```

示例要求 Python 3.10 或更高版本。运行前将 `path` 和 `record_index` 改为实际值，并先确认 `event.json` 表示事件已完成。
