# 天籁原始数据缓存：部署与联调手册

本手册对应当前源码，供部署人员和消色散后端联调使用。示例地址、端口和目录均为占位值，必须根据现场网络及存储规划替换；不能直接视为已完成部署或通过性能验收。

## 1. 工作流程与范围

```text
消色散后端 ──CAPTURE/TCP+JSON──> 控制器（存储服务器管理网）
                                │ PREPARE / STORE / RELEASE
                                ▼
前端 ──UDP数据──> 14台缓存服务器 ──28路ZeroMQ/TCP──> 单100G口落盘服务
                                                        │ CAPTURE_RESULT/TCP+JSON
                                                        └──────────────> 消色散后端
```

每个接收口正常使用两个各 50 秒的内存缓冲区。收到触发后，程序保护指定历史区间；在发送和写盘期间，另一主缓冲区继续接收。释放后需要重新积累，才恢复约 100 秒周期跨度。因此处理事件期间不能承诺持续保有最近完整 100 秒历史。

当前实现只接受已定稿的历史数据；单个事件上限为 5 秒，忙时不能接收第二个独立事件。`READY` 表示节点满足程序的接收范围和状态条件，并不证明零丢包或当前仍在接收；应结合现场收包监控检查。

## 2. 部署前准备

1. 确认 0～13 号缓存服务器的接收口、PCI 地址、内存、管理网和专用数据发送口；确认存储服务器的管理网地址、高速数据网地址及落盘目录。控制器与落盘服务部署在这台存储服务器上。
2. 确认中心站和远端的积分周期计数起点一致；所有节点及控制器使用同一采集编号。前端计数复位、重启或回绕时，停止旧任务并建立新的采集编号。
3. 核实Linux、g++、Boost头文件、yaml-cpp、spdlog（含fmt）开发库、Python 3.10+和生产环境的ixcap头文件及库。验证全路径MTU、光链路、内存和NUMA；地址及端口须由现场网络规划确定。
   管理网需要放通：后端到控制器TCP 18999、控制器到14台缓存节点TCP 19000、存储服务器到后端TCP 19001。
4. 保留 `Common/` 与 `F0-2/`、`F3-10/`、`F11-13/` 的相对目录；大小写与示例一致。原压缩包中的 `build/` 和可执行文件不代表当前源码已构建，应在目标环境重新编译。
5. 停止并检查将要清理的旧构建产物；项目根目录的 `make clean` 会删除三组构建目录、可执行文件及落盘服务构建产物，`make clean PROFILE=F3-10` 仅清理对应组。完成备份后再执行清理。

## 3. 配置和编译

每台服务器使用所属目录中的 `server_XX.yaml` 和 `receive_config.yaml`。主要配置如下：

也可使用 [Ansible 部署脚本](deploy/ansible/README.md)生成配置和安装服务。全部运行配置统一使用 YAML。

节点配置的 `controlBindIp` 已按部署说明填入 `172.0.0.128～141`，分别对应 tianlaistore1～14；`tools/nodes_example.json` 和 Ansible 示例清单也采用这些内网规划值。`172.0.0.204～217` 为对应 HDM 管理口地址，不应自动作为 SSH 或缓存程序监听地址。实际网卡配置及 SSH 可达性仍需核对。

| 文件 / 字段 | 要填写或核对的内容 |
| --- | --- |
| `receive_config.yaml` / `acquisitionId` | 全体节点与控制器一致，替换 `acq-CHANGE_ME`。 |
| `integrationPerTrace` / `periodMicroseconds` | 默认 50000 / 200；须与前端实际包头一致。 |
| `halfBufferSeconds` / `maxEventSeconds` | 默认 50 / 5；当前代码对事件长度有 5 秒上限。 |
| `transitionMiB` / `reserveMemoryMiB` | 默认每台 32768 / 49152 MiB；分别为全机过渡区及系统预留。 |
| `deviceNameN` / `cpuIdN` | 每个接收口实际 PCI 地址和 CPU 绑定；`-1` 表示不绑定。 |
| `controlBindIp` / `controlPort` / `controlPeerIp` | 节点管理网监听地址、端口及允许的控制器 IP；默认端口 19000。 |
| `sendEthName` / `sendSourceIp` | 单个存储数据网口及其无标签IP地址。 |
| `sendDestinationIp` / `sendDestinationPort0/1` | 存储100G口的单个IP；两个逻辑lane端口为`20000 + server_id`和`20100 + server_id`。 |
| 发送速率 | 不设置应用层限速，使用单个网口的可用带宽。 |
| `tools/nodes_example.json` | 改为所有 14 台节点的实际管理网地址和端口，并保存为现场配置。 |
| `logFilePath` | `-` 表示只输出 stderr；填文件路径则额外输出 50 MiB 轮转日志、最多保留 5 个备份。 |

从项目根目录编译：

```bash
make -j4 production
# 仅编译某一组：make -j4 production PROFILE=F3-10
```

ixcap 不在默认位置时使用 `IXCAP_INCLUDE` 和 `IXCAP_LIB` 指定路径。无 ixcap 的模拟构建使用 `make -j4 simulation`；也可加 `PROFILE=F0-2` 只构建一组。生产配置不包含 `simulationPortN`，使用 `--simulate` 前须为每个接收端口显式添加该参数。模拟程序不能替代真实网卡验证。

配置检查示例（其他节点替换目录及文件名）：

```bash
cd F0-2
./tianlai-store-f0-2 server_00.yaml receive_config.yaml --check-config
```

`--check-config` 不分配大缓存或初始化网卡，允许占位符；通过并不代表现场配置或硬件已验证。

## 4. 启动顺序

1. 确认落盘目录存在且有足够空间，在存储服务器编译并启动 C++ 接收器，例如：

   ```bash
   make storage
   ./Storage/tianlai-storage --config Storage/storage.yaml --check-config
   ./Storage/tianlai-storage --config Storage/storage.yaml
   ```

   启动前修改`Storage/storage.yaml`中的数据目录、100G口单IP、28个来源端点和后端通知地址；需要放通TCP 20000～20013及20100～20113。数据IP直接配置在100G物理接口上，不使用VLAN。

   默认端口分配如下，Ansible 模板会自动生成；手工配置时必须保持一致：

   | lane 0端口 | lane 1端口 | 缓存节点server_id |
   | --- | --- | --- |
   | 20000～20013 | 20100～20113 | 0～13，端口尾号与server_id对应 |

2. 启动各缓存节点。示例为 0 号，其他服务器使用各自目录和配置：

   ```bash
   cd F0-2
   sudo ./tianlai-store-f0-2 server_00.yaml receive_config.yaml
   ```

3. 在同一台存储服务器上启动一个控制器，`--bind` 使用该服务器的管理网 IP，`--allow-source` 填后端 IP，节点的 `controlPeerIp` 填控制器管理网 IP：

   ```bash
   python3 tools/controller.py --nodes tools/nodes_example.json \
     --acquisition acq-20260918-001 --journal /data/store-control \
     --bind 192.168.200.250 --allow-source 192.168.200.10
   ```

   此处采集编号和地址均为示例。控制器和落盘服务是同机的两个独立进程：控制器走管理网，落盘服务走高速数据网。控制器默认 `--period-us 200 --integration-per-trace 50000`，须与所有节点对应配置一致；程序不会自动验证此一致性。生产默认必须配置 0～13 全部节点；`--allow-partial` 只用于模拟或部分节点联调。不要启动两个控制器同时管理相同节点。

4. 消色散后端通过TCP/JSON监听19001，接收C++存储程序发来的写盘完成通知，并在同一连接回复确认。供联调的示例：

   ```bash
   python3 tools/result_listener.py --bind 192.168.200.10 --port 19001 \
     --allow-source 192.168.200.250
   ```

   示例程序打印后立即确认，仅供联调；生产后端应持久化处理结果后再确认。288路事件在节点0～10共22个lane形成持久化标记后发送`status=STORED`；384路事件需要节点0～13共28个lane。

## 5. 检查状态与发送触发

通过后端或`tools/trigger.py`发送带4字节长度前缀的TCP JSON。以下示例使用临时请求文件；请替换采集编号、控制器地址及事件编号。`trigger.py`可自动生成`request_id`。

```bash
cat > /tmp/tianlai-status.json <<'EOF'
{"version":1,"acquisition_id":"acq-20260918-001","command":"QUERY_STATUS","channel_count":288}
EOF
python3 tools/trigger.py --host 192.168.200.250 --file /tmp/tianlai-status.json
```

确认 `accept_trigger` 为 `true`，并确保所需起止周期位于返回的 `oldest_period` 与 `settled_end` 所示范围内；结束边界为不包含端点。然后提交触发：

```bash
cat > /tmp/tianlai-capture.json <<'EOF'
{"version":1,"acquisition_id":"acq-20260918-001","command":"CAPTURE","event_id":"event-000001","channel_count":288,"start":{"trace_period_id":120,"integration_period_id":45000},"period_count":15000}
EOF
python3 tools/trigger.py --host 192.168.200.250 --file /tmp/tianlai-capture.json
```

`channel_count`选择288或384；省略时默认384。默认每个积分周期200微秒，15000个周期为3秒。示例起点及长度仅说明格式，现场须选择当前有效的历史区间。首次回复`PREPARING`不表示已经写盘完成；可按事件编号查询：

触发中也可用 `"duration_ms":3000` 替换 `"period_count":15000`，两者不能同时填写；时长须精确对应整数个积分周期。完整字段、跨 Trace 结束位置与确认格式见 [协议说明](PROTOCOL.md)。

```bash
cat > /tmp/tianlai-event.json <<'EOF'
{"version":1,"acquisition_id":"acq-20260918-001","command":"QUERY_EVENT","event_id":"event-000001"}
EOF
python3 tools/trigger.py --host 192.168.200.250 --file /tmp/tianlai-event.json
```

常见状态：`PREPARING`正在保护、`PREPARED`表示全体已保护、`STORING`正在双lane回传、`STORED`表示全体已持久化、`RELEASING`正在统一释放、`COMPLETED`表示全部完成。检查`missing_by_server`以及每台节点的`lane-0.stored.json`和`lane-1.stored.json`。

## 6. 故障与恢复

| 现象 | 检查和处理 |
| --- | --- |
| `BUSY_OR_WARMING` / 未就绪 | 检查是否有活动事件、积累时长、节点收包及控制器状态；`READY` 本身不能证明收包仍在持续。 |
| `EXPIRED` | 所请求的起点已经超出保留历史；检查触发延迟和周期范围。 |
| `NOT_READY` | 结束周期尚未定稿或尚未收到；当前实现不会等待未来数据。 |
| `PAUSED` / `SEND_FAILED` | 查看事件错误、各节点状态、网络和存储；按协议使用 `RESUME` 协调重试。若日志磁盘写入失败，先修复日志存储，重启后核对日志和各节点实际状态。节点已 `REJECTED` 的事件不能简单靠 `RESUME` 重新准备，须先明确处理/取消旧事件，再以新事件编号提交。 |
| 后端未收到完成通知 | 288路检查节点0～10共22个lane，384路检查0～13共28个lane，以及`storage-complete.json`、存储服务日志和后端TCP 19001。 |
| 有`.bin`但事件未完成 | 部分文件不能作为完成凭据；检查每节点两个lane标记和控制器状态。 |

控制器日志可用于重启后恢复状态，但缓存数据仍在节点内存，缓存进程退出或掉电后无法从内存恢复。取消事件会放弃相应受保护数据，应由现场操作人员根据事件状态决定。

## 7. 验收

在 Linux 项目根目录运行 `make test`，其中包含无 ixcap 的模拟构建；`make help` 可查看其他命令。模拟测试使用缩小缓存和本地数据，不代表真实环境的持续吞吐。正式投运前应核对：14 台接收映射和周期对齐、断流/丢包监控、跨缓冲触发、传输重试、掉电及故障恢复、通知与去重，以及真实网卡、NUMA、可用内存和磁盘阵列持续写入能力。测试结果应记录环境、版本、时长和缺包数量。
