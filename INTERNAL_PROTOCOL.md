# Internal Protocol

本文供缓存、控制器和存储服务的开发与维护人员使用，描述系统内部控制和数据传输。消色散后端触发接口以及最终文件格式见 [PROTOCOL.md](PROTOCOL.md)；[积分记录格式](PROTOCOL.md#stored-files)同时用于传输数据块正文。

## Controller to Cache

控制器与缓存节点通过管理网 TCP + JSON 通信，节点默认端口为 19000。每个连接处理一次请求及回复，消息由 4 字节网络序 JSON 长度和 UTF-8 JSON 对象组成，JSON 最长 1200 字节。内部采集标识为 `tianlai`。节点只允许配置的 `controlPeerIp` 发起状态变更，同一管理子网允许查询状态。

`PREPARE` 使用 `server_mask` 携带参与节点位图；节点将位图写入数据元数据，供存储端判断事件完成范围。

| 命令 | 额外字段 | 行为 |
|---|---|---|
| `PREPARE` | `event_id`、`channel_count`、`server_mask`、`start`、`period_count` | 检查并异步保护缓存区间 |
| `STORE` | `event_id` | 启动两条发送通道；重复请求遵循当前事件状态 |
| `QUERY_EVENT` | `event_id` | 查询事件、缺包、发送和冻结指标 |
| `RELEASE` | `event_id` | 仅在本节点全部数据持久化后释放冻结区 |
| `CANCEL` | `event_id` | 对未发送或发送失败事件取消；准备中延后释放 |
| `QUERY_STATUS` | 无 | 查询状态、收数和共同周期范围 |
| `QUERY_PORT` | `port` | 查询单口周期范围、缺包和冻结指标 |

节点事件通常按 `PREPARING → PREPARED → SENDING → STORED → RELEASED` 变化；`SEND_FAILED` 保留冻结数据，`REJECTED` 和 `CANCELLED` 分别表示准备失败和取消。控制器在所有参与节点 `PREPARED` 后发送 `STORE`，并在所有参与节点 `STORED` 后发送 `RELEASE`。节点请求超时后重试；控制器重启后将未完成事件暂停，须通过 `RESUME` 核对后继续。

节点 `runtime_state` 与事件状态独立，包含内存准备、等待收数、缓存、发送和错误等阶段。`QUERY_STATUS` 提供 `data_flow`、接收口、最后收包年龄和运行期缺包统计；`QUERY_PORT` 提供单口统计。存储服务的实时状态文件默认为 `/run/tianlai-storage-status.json`，仅用于监控，不代替持久化完成标记。

## Cache to Storage

每台缓存服务器使用同一个物理发送口和源 IP 建立两条 ZeroMQ REQ 连接：lane 0 发送偶数接收口，lane 1 发送奇数接收口。存储端按 `serverId/lane/bindAddress/port/allowedSource` 校验入口；默认端口为 20000～20013 和 20100～20113。两条通道均确认后节点才进入 `STORED`。

每个会话依次发送：4 字节大端长度加 JSON 元数据、若干数据块、一个 END 块。除最终确认或已完成事件的幂等回复外，成功接收的数据块返回 ASCII `OK`；`OK` 只表示完成校验和写入，不表示数据已持久化。当前文件累计 1 GiB、经过 5 秒或写完时触发 `fdatasync`；END 时同步全部数据和完成标记后，返回长度前缀 JSON：

```json
{"version":1,"status":"STORED","event_id":"event-000001","acquisition_id":"tianlai","server_id":3,"lane":0}
```

发送端校验事件、采集编号、节点和 lane。失败时事件保持冻结；重试时已持久化 lane 可直接确认，未完成 lane 从头发送。TCP 确认、ZeroMQ 发送成功或块级 `OK` 均不等于最终持久化确认。

### TLSTORE1 Metadata

首条消息是 4 字节大端长度和 UTF-8 JSON，JSON 最长 1200 字节。例如：

```json
{
  "version": 1,
  "format": "TLSTORE1",
  "event_id": "event-000001",
  "acquisition_id": "tianlai",
  "server_id": 3,
  "lane": 0,
  "channel_count": 288,
  "server_mask": 15,
  "result_address": "172.0.0.1",
  "result_port": 19001,
  "port_count": 4,
  "ports": [0, 2],
  "start_period": 6045000,
  "period_count": 15000,
  "integration_per_trace": 50000,
  "period_microseconds": 200,
  "packet_bytes": 8848
}
```

`server_mask` 每位对应同编号节点；`port_count` 是该节点接收口总数，`ports` 是本 lane 负责的端口。`result_address` 和 `result_port` 是本次完成通知的地址和端口；不通知时分别为空字符串和 0。远端节点 11～13 额外携带：

```json
{"profile":"remote96","packets_per_board":2,"physical_board_count":12,"frequency_bins_per_port":86,"total_valid_frequencies":1024,"fft_per_packet":12}
```

远端会话的 `packet_bytes` 为 8272、`port_count` 为 4；频点与包位置映射见[最终文件格式](PROTOCOL.md#stored-files)。

### TLSTORE1 Blocks

每块默认 128 个积分周期，`chunkPeriods` 可配置为 1～128，最后一块可不足该值。块头为 40 字节，整数均为大端：

| 偏移 | 类型 | 字段 |
|---:|---|---|
| 0 | uint32 | 类型：1 为数据，2 为 END |
| 4 | uint32 | 本地接收口 |
| 8 | uint32 | `packetId`；远端为该口两个包号的起始号 |
| 12 | uint32 | `boardBegin` |
| 16 | uint64 | 本块起始绝对积分周期 |
| 24 | uint64 | 本块周期数 |
| 32 | uint32 | 正文长度 |
| 36 | uint32 | 保留字段，必须为 0 |

数据正文由连续积分记录构成，格式见 [PROTOCOL.md](PROTOCOL.md#stored-files)。END 的其余 36 字节必须为 0，没有正文。数据正文不计算应用层 CRC；TCP 提供传输校验和重传，但不检测内存、PCIe 或磁盘中的静默损坏。
