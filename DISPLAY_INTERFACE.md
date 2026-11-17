# Display Control Interface

显控程序连接控制器 `172.0.0.142:18999`。以下是界面需要的查询、触发和服务控制接口。

## 1. 通信方式

| 对象 | 默认地址 | 用途 |
|---|---|---|
| 控制器 | `172.0.0.142:18999` | 系统状态、触发、事件查询和服务启停 |
| 缓存节点 | `172.0.0.128～141:19000` | 网口及缺包详情 |

所有接口使用 TCP + JSON：每次连接发送一条请求、接收一条回复。消息格式为 **4字节无符号大端长度 + UTF-8 JSON**，JSON 长度为1～1200字节。

控制器请求至少包含 `version: 1` 和 `command`；`request_id` 可省略，由控制器生成。直接查询缓存节点时，仍需填写 `request_id` 和 `acquisition_id`。

触发或启停超时后，先查询状态，不能直接重发。需要安全重试触发时，自行填写唯一的 `request_id` 和 `event_id`，从同一来源 IP 重发完全相同的报文。

界面显示 `tianlaistore1`～`tianlaistore14`；接口使用 `server_id: 0`～`13`，标签数字 = `server_id + 1`。

## 2. 查询状态

### 2.1 系统状态

```json
{"version":1,"command":"QUERY_STATUS","channel_count":288}
```

`channel_count` 为288或384，默认384；分别使用前11台或全部14台缓存服务器。读取：

| 字段 | 含义 |
|---|---|
| `status`、`accept_trigger` | 仅 `READY` 且 `true` 时可触发 |
| `oldest_period`、`settled_end` | 共同稳定区间 `[oldest_period, settled_end)` |
| `node_runtime_states` | 各节点状态，键为内部编号 |
| `partial_node_set` | `true` 表示参与节点不足 |

默认每个 trace 为50000个周期：`trace = 绝对周期 // 50000`，`integration = 绝对周期 % 50000`。

回复示例（只列界面常用字段）：

```json
{"status":"READY","accept_trigger":true,"oldest_period":6000000,"settled_end":6250000,"channel_count":288,"partial_node_set":false}
```

此时可选的起点范围为 `[6000000, 6250000)`；保存3秒需要15000个周期，起点还须满足 `起点 + 15000 <= 6250000`。

### 2.2 单节点状态

如需网口详情，向对应缓存节点发送：

```json
{"version":1,"request_id":"node-001","acquisition_id":"tianlai","command":"QUERY_STATUS"}
```

| 字段 | 建议显示 |
|---|---|
| `runtime_state`、`uptime_ms` | 运行阶段、运行时间 |
| `receiving_ports` / `receive_ports` | 最近2秒有数据的端口数 / 总数 |
| `inactive_ports` | 无数据端口，编号从0开始 |
| `lost_packets`、`lost_by_port` | 本次运行累计缺包及各端口缺包 |

单口详情使用 `QUERY_PORT` 并增加 `port: 0`。系统 `READY` 不代表所有网口都有数据或零缺包。节点连接失败时，用第4节的 `CACHE_SERVICE` 查询服务状态。

回复字段示例：`{"server_id":0,"runtime_state":"CACHING_DATA","receiving_ports":7,"receive_ports":8,"inactive_ports":[3],"lost_packets":4}`，界面显示为 `tianlaistore1`、接收口 `7/8`、无数据口 `3`。

### 2.3 存储状态

```json
{"version":1,"command":"QUERY_STORAGE"}
```

外层 `status=READY` 只表示查询成功。实际状态读取 `storage.state`：`WAITING_FOR_DATA` 等待数据，`RECEIVING_AND_WRITING` 正在写盘，`NOTIFYING_BACKEND` 通知中，`NOTIFICATION_RETRY` 通知重试；`ERROR`、`OFFLINE` 表示异常或不可用。界面可显示 `listeners_ready/listener_count`、`active_transfers`、`event_completed_lanes/event_expected_lanes`、`event_written_bytes` 和 `last_error`。

回复示例（字段摘录）：

```json
{"status":"READY","storage":{"state":"WAITING_FOR_DATA","listeners_ready":28,"listener_count":28,"active_transfers":0,"last_error":""}}
```

## 3. 触发和查询事件

先用 `QUERY_STATUS` 确认可以触发，再从共同稳定区间选起点。以下保存288路、3秒数据：

```json
{
  "version": 1,
  "command": "CAPTURE",
  "channel_count": 288,
  "start": {"trace_period_id": 120, "integration_period_id": 45000},
  "duration_ms": 3000,
  "notify": false
}
```

`duration_ms` 和 `period_count` 二选一，最长50秒。默认每周期200微秒。控制器在回复中返回 `event_id`。

`notify: false` 表示通过查询等待，不接收回调。`PREPARING` 只表示已提交。保存 `event_id`，随后查询：

```json
{"status":"PREPARING","event_id":"event-example-001","channel_count":288}
```

```json
{"version":1,"command":"QUERY_EVENT","event_id":"event-example-001"}
```

`COMPLETED` 表示写盘及缓存释放完成；`PAUSED` 需要排查；`CANCELLED` 已取消。界面还应显示 `missing_by_server`，确认是否缺包。若暂未取得事件编号，可用 `QUERY_ACTIVE_EVENT` 查询当前未完成事件；已完成事件必须按 `event_id` 查询。

完成回复示例（字段摘录）：`{"status":"COMPLETED","event_id":"event-example-001","missing_by_server":{"0":0,"1":2}}`。界面应将编号 `1` 显示为 `tianlaistore2`。

## 4. 缓存服务启停

例如启动 `tianlaistore1`～`tianlaistore3`：

```json
{"version":1,"command":"CACHE_SERVICE","action":"START","server_ids":[0,1,2]}
```

`action` 支持 `START`、`STOP`、`RESTART`、`STATUS`；`server_ids` 为非空、不重复的内部编号数组。

外层 `status=OK` 不代表每个节点成功。逐项检查 `nodes` 中的 `status`、`state`、`error`。

回复示例：`{"status":"OK","action":"START","nodes":{"0":{"status":"OK","state":"active"},"1":{"status":"ERROR","state":"failed","error":"start failed"},"2":{"status":"OK","state":"active"}}}`。界面应分别显示 `tianlaistore1` 和 `tianlaistore3` 成功、`tianlaistore2` 失败。

停止或重启会清空内存缓存，界面需先让用户确认。默认不允许停止有未完成事件的节点；只有用户明确决定丢弃该事件时才传 `force: true`。
