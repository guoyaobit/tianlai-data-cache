# 触发控制和原始数据回传协议 V1

## 输入数据和积分编号

原始 16 B 业务头沿用 GPU 输入格式：

| 偏移 | 定义 |
|---|---|
| 0 高 4 位 | 有效标记，必须为 15 |
| 0 低 4 位、1、2 | 20 位 tracePeriodId，高字节在前 |
| 3 最高位 | ifTrace，跟踪标记，不是缓存触发 |
| 3 低 7 位、4、5 | 23 位 integrationPeriodId，配置默认范围 0～49999 |
| 6 | boardId，原系统 0～35，远端子站 36～47 |
| 7 | 原系统：GPU ID 0～39；远端子站：频段及 FFT 半段包号 0～23 |
| 8～15 | 原样保留 |

内部使用 `uint64_t absolutePeriod = trace * integrationPerTrace + integration`。
区间统一为 `[start, start + count)`。默认 200 us/周期、最大 25000 周期（5 秒）。
触发以信号采集编号为准，UDP 到达时间和服务器系统时间不参与选取数据。
仅接受已经定稿的历史区间；不等待未来未到的数据。

所有节点启动时使用同一 acquisition_id。控制器 `--integration-per-trace` 和 `--period-us` 必须分别与所有节点 `receive_config.yaml` 中的 `integrationPerTrace`、`periodMicroseconds` 一致，默认 50000 和 200；控制器不会自动验证节点配置的一致性。原始包没有 acquisition_id，它由采集管理约定。
远端和原系统须有相同的积分计数起点，才能使用同一区间代表同一物理时间；本版不做时钟偏移换算。
前端清零、重启或 trace 20 位回绕时，需协调停止旧任务，并以新 acquisition_id 重启缓存
程序重新积累。禁止跨任务/跨计数回绕触发，不能靠收到一个倒退的包推断新任务。

## 千兆内网 UDP 控制

一个 UDP 数据报一条 UTF-8 JSON 对象，最长 1200 B，不加 TCP 长度前缀。
节点只接受配置 controlPeerIp 的报文。控制器入口只接受 --allow-source 的地址。

所有请求携带：

```json
{
  "version": 1,
  "request_id": "req-000001",
  "acquisition_id": "acq-20260918-001",
  "command": "QUERY_STATUS"
}
```

ID 字段只能使用 ASCII 字母、数字、下划线、短横线，长度 1～64。
重复 JSON 字段在控制器和节点端均被拒绝；节点端不接受数组。计数及长度须是 JSON 整数，不接受小数、布尔值或负数。
节点回复携带 version、request_id 和 server_id。控制器回复携带 version、request_id。
错误回复为 `status=ERROR, error=原因`。

request_id 用于请求/回复匹配，event_id 用于执行去重。同一事件编号只能对应同一区间。
超时重发同一请求；收到 PREPARING 后改用 QUERY_EVENT 轮询，不反复创建新事件。
节点状态不会因为重复 COMMIT/SEND/RELEASE 而倒退。先收到 CANCEL 的未知事件会留下
取消记录，防止迟到 PREPARE 再次启动该事件。

### 外部 → 控制器（默认 UDP 18999）

```json
{
  "version": 1,
  "request_id": "req-000002",
  "acquisition_id": "acq-20260918-001",
  "command": "CAPTURE",
  "event_id": "event-000001",
  "channel_count": 288,
  "start": {"trace_period_id": 120, "integration_period_id": 45000},
  "period_count": 15000
}
```

示例以288路模式保存3秒，包含`(120,45000)`到`(121,9999)`，不包含`(121,10000)`。
`channel_count`只能为288或384：288使用节点0～10，384使用节点0～13。省略该字段时按384处理，以兼容旧触发端。同一`event_id`不能改变通道数。

也可用 `"duration_ms":3000` 代替 `"period_count":15000`；**必须且只能提供其中一个**。毫秒时长须为正整数，换算后恰好是整数个积分周期；例如默认 200 微秒时，1 毫秒为 5 个周期。最大时长 5 秒；节点若配置更小的 `maxEventSeconds` 则按节点限制拒绝。控制器将请求规范化为 `period_count`，因此相同事件 ID、起点和等价长度可以重传，范围变化则冲突。

控制器保存和查询的事件还包含 `end_exclusive`、`period_microseconds` 和 `integration_per_trace`。结束位置为排他边界，并非最后一个包含的周期。例如上例为 `{"trace_period_id":121,"integration_period_id":10000}`。若终点恰好到达计数回绕边界，排他端点可为 trace `1048576`、integration `0`；该值仅为边界，不能作为新的触发起点。旧日志中已有的事件可能没有这些新增字段。

| command | 额外字段 | 含义 |
|---|---|---|
| CAPTURE | event_id、channel_count、start、period_count | 创建288或384路事件，通知对应节点保护数据 |
| QUERY_EVENT | event_id | 查询事件准备/传输/完成状态 |
| QUERY_STATUS | channel_count（可选，默认384） | 查询所选模式节点共同的可用时间范围和 accept_trigger |
| RESUME | event_id | 对 PAUSED 事件查询节点实际状态后继续 |
| CANCEL | event_id | 明确放弃已暂停事件；正在发送的节点不能直接取消 |

流程：PREPARING → ACCEPTED → SENDING → COMPLETED；任何不确定的失败转 PAUSED。
ACCEPTED 可能短暂存在，查询得到 SENDING 也表示全部节点已保护。
COMPLETED 只表示事件保存完成，不代表已经恢复完整 100 秒历史；再次触发应先查 READY。
部分节点失败或超时，不自动假定那些节点没有执行过请求，也不自动删除其他节点的数据。
控制器 `QUERY_STATUS` 只有在所有节点可触发且共同区间 `oldest_period < settled_end` 时才返回 READY；即使 READY，目标区间仍需落在共同区间内，节点可能因区间过期或未定稿而拒绝。控制器先检查包头计数的位宽，节点再按实际 `integrationPerTrace` 检查积分编号和范围。
事件日志写入失败时不会在内存中发布未成功记录的状态；无法写入暂停状态时仍在内存中暂停，重启后须结合日志和各节点状态人工核对，不能假定失败操作未执行。

### 写盘完成主动通知

C++存储服务根据事件`channel_count`确认节点0～10（288路）或0～13（384路）各自两个lane全部持久化且元数据一致后，先写入事件级`storage-complete.json`，再通过UDP通知消色散后端：

```json
{"version":1,"command":"CAPTURE_RESULT","status":"STORED","acquisition_id":"acq-20260918-001","event_id":"event-000001","channel_count":288,"start":{"trace_period_id":120,"integration_period_id":45000},"end_exclusive":{"trace_period_id":121,"integration_period_id":10000},"period_count":15000,"period_microseconds":200,"integration_per_trace":50000,"missing_by_server":{"0":0,"1":0,"2":0,"3":0,"4":0,"5":0,"6":0,"7":0,"8":0,"9":0,"10":0}}
```

`STORED`精确表示所选模式的11台或14台数据均已写盘并形成持久化标记；它不表示控制器已经完成缓存释放。缺包数量见`missing_by_server`，因此该通知也不是零丢包保证。

后端应按 `(acquisition_id,event_id)` 幂等地持久化并处理结果，再使用监听通知的同一个 UDP 套接字，向收到报文的来源 IP 和临时端口回复：

```json
{"version":1,"command":"ACK_RESULT","status":"ACKNOWLEDGED","acquisition_id":"acq-20260918-001","event_id":"event-000001"}
```

存储服务校验确认报文的来源 IP、来源端口、命令、状态、采集编号和事件编号，全部匹配后才写入 `backend-acknowledged.json`。每次发送从 `backend.bindAddress` 分配一个临时源端口；后端固定监听默认 UDP 19001，并向实际来源地址回复。超时或重启后，存储服务会从 `storage-complete.json` 继续重试，因此后端必须处理重复通知。`tools/result_listener.py` 是演示接收器，打印后立即确认，不提供业务侧持久化。控制器不再转发完成通知，也不处理 `ACK_RESULT`。若事件暂停，仍通过控制器的 `QUERY_EVENT` 查询并按 `RESUME` 或 `CANCEL` 处理。

### 控制器 → 节点（默认 UDP 19000）

PREPARE字段与CAPTURE相同，仅command不同；控制器只向所选模式包含的节点发送。

| command | 额外字段 | 允许行为 |
|---|---|---|
| PREPARE | event_id、channel_count、start、period_count | 检查模式和历史区间并异步保护，返回PREPARING/PREPARED |
| COMMIT | event_id | 全部节点准备后，由控制器转为 QUEUED |
| SEND | event_id | QUEUED或SEND_FAILED时并行启动两个不限速ZeroMQ lane；忙时不重复启动 |
| QUERY_EVENT | event_id | 查询事件状态、missing_packets、sent_bytes、freeze_ms、send_ms、send_rate_bps、过渡区峰值和 error |
| RELEASE | event_id | 只有该节点全部接收口数据可靠落盘后，即 STORED 状态，才允许释放冻结区 |
| CANCEL | event_id | 未发送或发送失败时放弃事件；PREPARING 时延后安全释放 |
| QUERY_STATUS | 无 | READY/BUSY/WARMING、共同周期范围、最繁忙口过渡区占用/容量/警告、最近冻结耗时等 |
| QUERY_PORT | port | 单口周期范围、收包计数、过渡区占用/容量/峰值/警告、最近冻结耗时和冻结次数 |

节点主要状态：PREPARING → PREPARED → QUEUED → SENDING → STORED → RELEASED。
发送失败为 SEND_FAILED，仍保护数据；准备失败为 REJECTED；明确取消为 CANCELLED。
释放后历史不足时节点 QUERY_STATUS 返回 WARMING，而不是 READY。

控制器只有在所选模式全部节点PREPARED后才授权发送。288路并行11个节点，384路并行14个节点；每节点使用两个ZeroMQ连接，任一lane失败都会使节点进入SEND_FAILED并继续保护数据。
默认 RPC 重试间隔 200 ms、5 次；准备/发送长任务通过查询等待。
调度器重启时读取日志，未完成事件先 PAUSED；RESUME 根据节点现状继续，不盲目重放 SEND。

## ZeroMQ 数据协议 TLSTORE1

数据通道和管理网UDP控制通道独立。每台缓存服务器建立lane 0和lane 1两个ZeroMQ REQ连接，两个连接绑定同一个物理网口和源IP。lane 0只发送偶数接收口，lane 1只发送奇数接收口。`storage.receivers`必须显式列出28个`serverId/lane/bindAddress/port/allowedSource`组合；存储端据此启动28个ROUTER监听线程。默认端口为20000～20013和20100～20113。任一端点不匹配都会拒绝会话。

每个 ZeroMQ 请求是一条消息：先发送带 4 字节长度的 JSON 元数据，然后逐条发送 40 字节块头和正文，最后发送 40 字节 END。通常除 END 外每条消息由存储端成功校验并写入后回复 ASCII OK；该 OK 不是持久化确认。存储端累计 256 MiB 或经过 1 秒后批量 `fdatasync`，END 在全部文件及完成标记持久化后回复 4 字节长度前缀的 STORED JSON。若相同元数据对应的持久化标记已经存在，首条元数据消息直接返回 STORED，发送端不再重传数据。发送端验证事件、采集编号、节点和 lane 后才进入 STORED。消息排队、块级 OK、TCP ACK 或 ZeroMQ send 成功均不代表完整落盘。

每次SEND为两个lane分别建立新会话。未完成lane从头重传；已有持久化标记的lane直接返回STORED。同一server/lane不可并发写，但同一server的两个lane可以并行写入互不重叠的端口文件。

发送端不做应用层限速，两个lane竞争同一物理网口的可用带宽。TCP负责拥塞控制和重传；部署时仍需验证14台节点汇聚到单100G接收口及磁盘阵列时的持续吞吐。

### TLSTORE1 消息内容

首条消息为 4 B 大端 JSON 长度及 UTF-8 元数据，最大 1200 B。元数据包括：

```json
{
  "version": 1,
  "format": "TLSTORE1",
  "event_id": "event-000001",
  "acquisition_id": "acq-20260918-001",
  "server_id": 3,
  "lane": 0,
  "channel_count": 288,
  "port_count": 4,
  "ports": [0, 2],
  "start_period": 6045000,
  "period_count": 15000,
  "integration_per_trace": 50000,
  "period_microseconds": 200,
  "packet_bytes": 8848
}
```

`channel_count`必须与触发一致。288路只允许server 0～10，384路允许server 0～13。`port_count`仍表示节点接收口总数；`ports`列出本lane实际发送的端口，lane 0必须为偶数口，lane 1必须为奇数口。两个列表互斥且并集覆盖全部接收口。

随后逐接收口发送数据块，每块默认 64 个积分周期，最后一块可以不足 64 个。
块头共 40 B，所有整数大端，禁止直接发送带本机填充的 C++ struct：

| 偏移 | 类型 | 字段 |
|---:|---|---|
| 0 | uint32 | type，1=data，2=end |
| 4 | uint32 | 本地接收 port |
| 8 | uint32 | packetId；远端子站为该口两个包号中的偶数起始号 |
| 12 | uint32 | boardBegin |
| 16 | uint64 | 本块 startPeriod |
| 24 | uint64 | 本块 periodCount |
| 32 | uint32 | bodyBytes |
| 36 | uint32 | 保留字段，必须为0；不计算应用层CRC |

type=2 时其余 36 B 必须为零，没有 body，表示该 lane 所有数据已发完。
数据正文不计算应用层CRC，依赖TCP校验、重传和ZeroMQ消息边界保证传输完整性；程序仍严格校验消息长度、周期、端口映射和原始包头。内存、PCIe或磁盘静默损坏不在本协议检测范围内。
数据块 body 是连续的积分记录，每个记录为：

| 大小 | 内容 |
|---:|---|
| 8 B | absolutePeriod，大端 |
| 8 B | boardMask，大端，每个包位置一位；原系统每板一位，远端每板两位 |
| 4 B | packetBytes，大端，为 2320、8848 或远端的 8272 |
| 4 B | packetSlotCount（原名 boardCount），原系统 36/18，远端 24 |
| packetSlotCount × packetBytes | 按包位置排列的原始 16 B 业务头 + 原始数据 |

缺包的整个原始包位置输出零且对应位图为 0；整周期缺失输出完整记录但位图为 0。
没有把压缩复数展开为浮点，也没有混用 GPU Stokes 输出格式。

### 远端子站 remote96 扩展

11～13 节点使用相同的 TLSTORE1 二进制结构，但在会话元数据中必须额外携带：

```json
{
  "profile": "remote96",
  "packets_per_board": 2,
  "physical_board_count": 12,
  "frequency_bins_per_port": 86,
  "total_valid_frequencies": 1024,
  "fft_per_packet": 12
}
```

这些字段与 event_id 等会话字段合在同一 JSON 中，不是独立数据报。packet_bytes=8272、
port_count=4。三类节点共用本 ZeroMQ 协议；原积分记录文件结构保留，旧版接收器不兼容。

对本地口 p，全局频段号 g=(server_id−11)×4+p，块头 packetId=2g、boardBegin=36。
周期记录固定 24 个包位置，slot=(boardId−36)×2+(rawPacketId−2g)。
每板先存偶数包（FFT 0～11），再存奇数包（FFT 12～23）；完整位图为 0xFFFFFF。
存储端按 slot 验证原始 boardId=36+slot/2、packetId=2g+slot%2，并分别统计缺失的半段包。
不合并或修改两个原始包头。

包内为 `[12 FFT][86 频点][8 通道]`，完整积分编号仍由 trace/integration 决定。
全局频点=g×86+包内频点；g=11 时，仅本地频点 0～77 有效，78～85 是 **8** 个填充频点。
填充位置原样保留，不作为缺包，不强行覆盖非零原始内容。

### 落盘确认

C++ 存储接收器检查长度、区间、映射、每个有效包的头字段及每个缺包位置的零填充；Python 实现保留为协议参考。
数据按 `acquisition/event/server-N/port-P.bin` 存储，文件内容是上述积分记录连续排列。
旁边保存lane元数据和`.stored.json`，记录各端口缺包数量，不计算或保存文件CRC。
只有 lane 所有文件内容和目录持久化完成后，才通过 ZeroMQ 回复长度前缀 JSON：

```json
{
  "version": 1,
  "status": "STORED",
  "event_id": "event-000001",
  "acquisition_id": "acq-20260918-001",
  "server_id": 3,
  "lane": 0
}
```

节点收到单路匹配的 STORED（覆盖本机全部接收口）才转为 STORED；UDP RELEASE 不能伪造这一状态跃迁。
会话失败重试从本节点事件开头发送，接收端按固定偏移写入，元数据不同则拒绝覆盖。
同节点同 lane 不允许并发会话写文件。接收器重启允许重传；没有磁盘成功确认的数据
仍保存在节点 RAM，除非显式取消或节点进程退出。
