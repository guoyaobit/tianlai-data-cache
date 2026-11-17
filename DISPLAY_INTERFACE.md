# Display Control Interface

本文供显控开发人员使用。显控节点已部署 `tlcache` 和 `tltrigger`；程序可以执行命令并读取标准输出中的 JSON，无需自行实现 TCP 报文。

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

示例仅列常用字段。`server_id:0` 对应 `tianlaistore1`，依次到 `server_id:13` 对应 `tianlaistore14`。界面建议显示：

节点停止或异常时，`tlcache --json` 可能返回非零退出码，但标准输出仍包含状态 JSON；显控程序应读取该 JSON 并显示异常节点。

| 字段 | 显示内容 |
|---|---|
| `storage.state` | 存储状态；`RECEIVING_AND_WRITING` 为正在写盘，`WAITING_FOR_DATA` 为等待数据 |
| `storage.event_written_bytes / event_expected_bytes` | 本次事件的估算写盘进度；重传时可能重复计数 |
| `storage.event_completed_lanes / event_expected_lanes` | 已完成 / 应完成的传输通道 |
| `storage.last_error` | 最近错误 |
| `cache_nodes[].runtime_state`、`uptime_ms` | 节点运行阶段和运行时间 |
| `receiving_ports / receive_ports` | 最近 2 秒有数据的网口数 / 总网口数 |
| `inactive_ports`、`lost_by_port` | 无数据网口和各口累计缺包 |

终端人工查看可直接运行 `tlcache`；持续刷新使用 `tlcache --watch 2`。

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

| `status` | 含义 |
|---|---|
| `COMPLETED` | 写盘及缓存释放完成 |
| `PAUSED` | 保存中断，需排查 |
| `CANCELLED` | 已取消 |

`COMPLETED` 后还需查看 `missing_by_server`：值大于 0 表示该节点有缺包。查询超时不等于触发失败，不能直接重复触发；先用已取得的 `event_id` 查询。需要指定历史起点时，可用 `tltrigger 3 --288 --period 7010000 --json`，其中 `--period` 后是绝对积分周期。

## 4. 启停缓存节点

```bash
tlcache start 1-3 --json
tlcache stop 1-3 --yes --json
tlcache restart 2 --yes --json
```

编号 1～14 对应机柜标签 `tianlaistore1`～`tianlaistore14`；也支持 `all` 和 `2,5-7`。显控界面在停止或重启前应提示用户：内存中的缓存数据将丢失。`--yes` 表示界面已完成确认。

回复顶层 `status:OK` 不保证每台都成功，要逐项检查 `nodes` 中的 `status`、`state` 和 `error`。停止返回 `ACCEPTED`、`stopping` 表示已提交；继续运行 `tlcache --json` 确认节点变为 `STOPPED`。未完成事件会阻止停止或重启；只有用户明确要求丢弃事件时才增加 `--force`。
