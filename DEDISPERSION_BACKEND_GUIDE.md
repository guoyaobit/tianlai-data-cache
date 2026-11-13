# Dedispersion Backend Trigger Guide

本文面向消色散后端使用人员，说明如何检查缓存、发起保存、等待完成，以及如何确认存储服务器的完成通知。日常操作不需要填写积分周期，也不需要编写JSON。

## 使用位置

在已经安装`tltrigger`且能够访问管理网`172.0.0.142:18999`的节点执行命令。默认使用384路模式，单次可保存最近1～50秒的数据。

## 最简操作

先确认缓存可触发：

```bash
tltrigger
```

只有看到以下字段时才可以触发：

```json
"status": "READY",
"accept_trigger": true
```

保存最近3秒的384路数据，并等待整个事件结束：

```bash
tltrigger 3
```

保存最近50秒：

```bash
tltrigger 50
```

使用288路模式：

```bash
tltrigger status --channels 288
tltrigger 3 --channels 288
```

命令会自动选择全部参与节点共同缓存中最新且已经稳定的数据，不需要用户提供trace或integration编号。

## 判断是否完成

触发时会输出唯一事件编号，例如：

```text
event=event-20261113-045012-a1b2c3
```

使用数字短命令时，命令默认持续查询。最终返回：

```json
"status": "COMPLETED"
```

表示所选缓存节点已经完成发送，存储服务器已经持久化数据，缓存节点也已经释放本次事件。命令退出码为0。

若使用高级形式`tltrigger capture 3`只提交而不等待，可直接用事件编号查询：

```bash
tltrigger event-20261113-045012-a1b2c3
```

也可等待已有事件结束：

```bash
tltrigger wait event-20261113-045012-a1b2c3
```

状态顺序通常为：

```text
PREPARING → PREPARED → STORING → STORED → RELEASING → COMPLETED
```

`PAUSED`表示需要运维人员排查并决定恢复或取消；它不表示保存成功。

```bash
tltrigger resume EVENT_ID
tltrigger cancel EVENT_ID
```

## 再次触发

`COMPLETED`只表示上一次事件结束。缓存释放后需要重新积累历史数据，再次触发前仍应执行：

```bash
tltrigger
```

只有重新返回`READY`和`accept_trigger=true`时才能发起下一次保存。

## 接收存储完成通知

需要主动完成通知时，触发请求增加`"result_port":19001`。控制器自动使用触发TCP连接的真实来源IP，存储服务器完成写盘后连接该IP和端口发送`CAPTURE_RESULT`，无需配置固定后端IP。省略该字段时使用`tltrigger EVENT_ID`或`QUERY_EVENT`查询结果。

命令行触发并要求主动通知：

```bash
tltrigger capture 3 --result-port 19001
```

仓库中的`tools/result_listener.py`是单文件接收器，只依赖Python 3。先在存储服务器确认连接后端时使用的源地址：

可从项目根目录将该单文件复制到后端：

```bash
scp tools/result_listener.py ubuntu@BACKEND_HOST:~/result_listener.py
```

```bash
ip route get BACKEND_IP
```

记下输出中的`src`IPv4，然后在消色散后端启动：

```bash
python3 result_listener.py \
  --bind 0.0.0.0 \
  --port 19001 \
  --allow-source STORAGE_SOURCE_IP \
  --journal /data/tianlai-completions
```

确认端口正在监听：

```bash
ss -lntp | grep ':19001'
```

接收器先将通知写入`/data/tianlai-completions/EVENT_ID.json`并同步目录，再回复确认。重复通知内容一致时直接幂等确认，内容冲突时拒绝确认。

```json
{
  "version": 1,
  "command": "CAPTURE_RESULT",
  "status": "STORED",
  "event_id": "event-20261113-045012-a1b2c3",
  "channel_count": 384,
  "period_count": 15000,
  "missing_by_server": {"0": 0, "1": 0}
}
```

后端必须先持久化该结果，再在同一TCP连接回复长度前缀JSON：

```json
{
  "version": 1,
  "command": "ACK_RESULT",
  "status": "ACKNOWLEDGED",
  "event_id": "event-20261113-045012-a1b2c3"
}
```

存储服务器没有收到确认时会继续重试，因此后端必须按`event_id`实现幂等处理。`tltrigger`返回`COMPLETED`表示数据保存和缓存释放完成，不等于后端已经回复`ACK_RESULT`。

## 常见问题

- `BUSY_OR_WARMING`：至少一个参与节点尚未缓存足够数据、没有收数，或存在未完成事件。
- `cache is not ready for a trigger`：先运行`tltrigger`，根据节点状态排查。
- `PAUSED`：不要创建新事件；先查询该事件并由运维人员执行恢复或取消。
- `completion TCP connect failed`：数据可能已经落盘，但存储服务器无法连接本次触发来源IP的`result_port`。确认接收器正在监听，并从存储服务器执行`nc -vz BACKEND_IP 19001`。
- 命令断开或超时：事件可能仍在后台执行，使用打印出的`event_id`查询，不要直接使用新编号重复触发。
