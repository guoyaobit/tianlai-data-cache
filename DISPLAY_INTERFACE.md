# Display Control Interface

本文供显控开发人员对接缓存系统，地址以现场配置为准。

## 1. 通信方式

| 对象 | 默认地址 | 用途 |
|---|---|---|
| 显控节点 | `172.0.0.61` | 显控程序运行位置 |
| 控制器 | `172.0.0.142:18999` | 系统状态、触发、事件查询和服务启停 |
| 缓存节点 | `172.0.0.128～141:19000` | 单节点收数、网口及缺包状态 |

所有接口使用 TCP + JSON：每次连接发送一条请求、接收一条回复。消息格式为 **4字节无符号大端长度 + UTF-8 JSON**，JSON 长度为1～1200字节。

请求包含 `version: 1`、`request_id` 和 `command`，回复中的 `request_id` 必须与请求一致。`request_id` 为1～64位 ASCII 字母、数字、下划线或短横线。每次新的查询使用新编号；超时重试同一操作时，使用原编号和完全相同的报文。

**超时不代表操作未执行**：触发或启停超时后，应先查询事件或服务状态。

界面显示 `tianlaistore1`～`tianlaistore14`；报文使用内部数字编号0～13。例如 `tianlaistore1` 对应 `server_id: 0`。转换规则：标签数字 = `server_id + 1`。下列 JSON 中的数字编号应在界面转换为标签显示。

## 2. 查询状态

### 2.1 系统共同状态

向控制器发送：

```json
{"version":1,"request_id":"status-001","command":"QUERY_STATUS","channel_count":288}
```

`channel_count` 可为288或384，默认为384；288路使用 `tianlaistore1`～`tianlaistore11`，384路使用1～14。主要回复字段：

| 字段 | 含义 |
|---|---|
| `status`、`accept_trigger` | `READY` 且 `accept_trigger=true` 时可触发 |
| `oldest_period`、`settled_end` | 共同稳定区间 `[oldest_period, settled_end)`；最新稳定周期为 `settled_end - 1` |
| `node_runtime_states` | 各节点运行状态，键为内部编号 |
| `participating_server_ids`、`partial_node_set` | 实际参与节点；`partial_node_set=true` 表示结果不完整 |

默认每个 trace 为50000个周期：`trace = 绝对周期 // 50000`，`integration = 绝对周期 % 50000`。实际参数以部署配置为准。

### 2.2 单节点状态

向对应缓存节点发送；节点接口需要 `acquisition_id`：

```json
{"version":1,"request_id":"node-001","acquisition_id":"tianlai","command":"QUERY_STATUS"}
```

| 字段 | 建议显示 |
|---|---|
| `server_id` | 校验与目标节点一致后，转换为 `tianlaistore` 标签 |
| `runtime_state`、`uptime_ms` | 运行阶段、运行时间 |
| `receiving_ports`、`receive_ports` | 最近2秒有数据的端口数 / 配置总数，例如 `7/8` |
| `inactive_ports` | 无数据端口，编号从0开始 |
| `lost_packets`、`lost_by_port` | 本次运行累计缺包及各端口缺包 |
| `oldest_period`、`settled_end` | 本节点稳定周期范围，结束位置不包含在范围中 |
| `event_state` | 节点事件状态 |

也可将 `command` 改为 `QUERY_PORT` 并增加 `port: 0` 查询单口详情。节点状态只允许控制器或与节点同一 `/24` 管理子网的来源查询。进程重启后累计计数清零。

系统 `READY` 不保证所有网口当前有数据或零丢包，应同时检查单节点的接收口和缺包信息。节点接口连接失败不一定是服务停止，可用第4节的 `CACHE_SERVICE` 查询服务状态。

### 2.3 存储状态

向控制器发送：

```json
{"version":1,"request_id":"storage-001","command":"QUERY_STORAGE"}
```

外层 `status=READY` 只表示查询已处理，实际服务状态读取 `storage.state`：`STARTING` 启动中，`WAITING_FOR_DATA` 等待数据，`RECEIVING_AND_WRITING` 正在写盘，`NOTIFYING_BACKEND` 通知中，`NOTIFICATION_RETRY` 通知重试，`ERROR`、`STOPPING`、`OFFLINE` 表示异常或不可用。

可显示 `listeners_ready/listener_count`、`active_transfers`、`event_id`、`event_completed_lanes/event_expected_lanes`、`event_written_bytes`、`write_rate_bytes_per_second` 和 `last_error`。带 `_total` 的字段是服务启动以来的累计值。

## 3. 触发和查询事件

先查询共同状态，再选择稳定范围内的数据。以下请求保存288路、3秒数据：

```json
{
  "version": 1,
  "request_id": "capture-001",
  "command": "CAPTURE",
  "event_id": "event-001",
  "channel_count": 288,
  "start": {"trace_period_id": 120, "integration_period_id": 45000},
  "duration_ms": 3000,
  "notify": false
}
```

`event_id` 必须全局唯一；`duration_ms` 和 `period_count` 二选一，最长50秒。默认每周期200微秒，上例为15000周期，结束边界是 `(121,10000)`，不包含该周期。各数据源必须使用相同的积分计数起点。

`notify: false` 表示显控通过查询等待，不接收主动回调。`PREPARING` 只表示已提交；发送以下请求查询进度，每次查询使用新的 `request_id`：

```json
{"version":1,"request_id":"event-query-001","command":"QUERY_EVENT","event_id":"event-001"}
```

`COMPLETED` 表示写盘及缓存释放完成；`PAUSED` 表示需要排查；`CANCELLED` 表示已取消。保存完成不等于零缺包，界面应显示 `missing_by_server`，并将编号转换为 `tianlaistore` 标签。

## 4. 缓存服务启停

例如选择 `tianlaistore1`～`tianlaistore3` 启动，转换为内部编号后向控制器发送：

```json
{"version":1,"request_id":"service-001","command":"CACHE_SERVICE","action":"START","server_ids":[0,1,2],"force":false}
```

`action` 支持 `START`、`STOP`、`RESTART`、`STATUS`；`server_ids` 为非空、不重复的内部编号数组。接口仅允许授权来源，默认包括显控节点 `172.0.0.61`。

回复外层 `status=OK` 不代表所有节点成功：应逐项检查 `nodes` 中的 `status`、`state` 和可选的 `error`，并将键 `"0"`、`"1"` 等转换为界面标签。`errors_omitted=true` 表示部分错误详情被省略，需查看节点日志。

停止或重启会清空内存缓存，界面应先要求用户确认。`force` 默认 `false`；设为 `true` 会绕过活动事件保护，可能丢失事件数据。接口按来源 IP 限制访问，不提供用户身份认证，应在受控管理网络使用。
