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
2. 确认中心站和远端的积分周期计数起点一致。前端计数复位、重启或回绕时，停止旧任务并重启缓存重新预热；后续事件继续使用全局唯一的新`event_id`。
3. 核实Linux、g++、Boost头文件、yaml-cpp、spdlog（含fmt）开发库、Python 3.10+和生产环境的ixcap头文件及库。验证全路径MTU、光链路、内存和NUMA；地址及端口须由现场网络规划确定。
   管理网需要放通：后端到控制器TCP 18999、控制器到14台缓存节点TCP 19000、存储服务器到后端TCP 19001。
4. 保留 `Common/` 与 `F0-2/`、`F3-10/`、`F11-13/` 的相对目录；大小写与示例一致。原压缩包中的 `build/` 和可执行文件不代表当前源码已构建，应在目标环境重新编译。
5. 停止并检查将要清理的旧构建产物；项目根目录的 `make clean` 会删除三组构建目录、可执行文件及落盘服务构建产物，`make clean PROFILE=F3-10` 仅清理对应组。完成备份后再执行清理。

## 3. 配置和编译

每台服务器使用所属目录中的 `server_XX.yaml` 和 `receive_config.yaml`。主要配置如下：

也可使用 [Ansible 部署脚本](deploy/ansible/README.md)生成配置和安装服务。全部运行配置统一使用 YAML。

节点配置的 `controlBindIp` 已按部署说明填入 `172.0.0.128～141`，分别对应 tianlaistore1～14；`tools/nodes_example.json` 和 Ansible 示例清单也采用这些内网规划值。`172.0.0.204～217` 为对应 HDM 管理口地址，不应自动作为 SSH 或缓存程序监听地址。实际网卡配置及 SSH 可达性仍需核对。

BMC/HDM带外电源控制脚本位于[`deploy/bmc/bmc_power.sh`](deploy/bmc/bmc_power.sh)，使用说明和安全注意事项见[`deploy/bmc/README.md`](deploy/bmc/README.md)。账号和密码不写入仓库；GPU服务器的BMC地址因原始文档未提供而未加入地址表。

| 文件 / 字段 | 要填写或核对的内容 |
| --- | --- |
| `acquisition_id` | 内部固定为`tianlai`，不再写入缓存YAML。 |
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

1. 确认`/data`已挂载到目标磁盘阵列且有足够空间，在存储服务器编译并启动 C++ 接收器，例如：

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

3. 在同一台存储服务器上启动一个控制器，`--bind` 使用该服务器的管理网 IP。控制器接受所有能够连接该管理地址的触发来源，节点的 `controlPeerIp` 填控制器管理网 IP：

   ```bash
   python3 tools/controller.py --nodes tools/nodes_example.json \
     --journal /data/store-control \
     --bind 172.0.0.142
   ```

   此处地址为示例。控制器内部固定使用`tianlai`，外部请求不填写采集编号。控制器和落盘服务是同机的两个独立进程：控制器走管理网，落盘服务走高速数据网。控制器默认 `--period-us 200 --integration-per-trace 50000`，须与所有节点对应配置一致；程序不会自动验证此一致性。生产默认必须配置 0～13 全部节点；`--allow-partial` 只用于模拟或部分节点联调。不要启动两个控制器同时管理相同节点。

4. 消色散后端通过TCP/JSON监听19001，接收C++存储程序发来的写盘完成通知，并在同一连接回复确认。供联调的示例：

   ```bash
   python3 tools/result_listener.py --bind 192.168.200.10 --port 19001 \
     --allow-source 172.0.0.142
   ```

   示例程序打印后立即确认，仅供联调；生产后端应持久化处理结果后再确认。288路事件在节点0～10共22个lane形成持久化标记后发送`status=STORED`；384路事件需要节点0～13共28个lane。

## 5. 检查状态与发送触发

在存储/控制服务器上可直接汇总查看所有已启用缓存节点，`--watch 2`表示每2秒刷新：

```bash
tlcache
tlcache --watch 2
tlcache period
tlcache start all
tlcache stop 1-3
tlcache restart 2,5-7
```

`tlcache`可在存储服务器、任意缓存节点或显控节点执行状态查询。它先显示存储服务状态，再显示缓存节点当前是否在收数据。`UPTIME`以秒、分、小时或天显示缓存进程本次启动后的运行时间。`DATA`为`RECEIVING`表示所有接收口在2秒内有数据，`PARTIAL`表示只有部分口有数据，`NO_DATA`表示所有口均已超过2秒未见数据。`RX_PORTS`显示活跃口数/总口数，`AGE_MS`是最久未收到数据的已启动端口时长，`NO_DATA_PORTS`直接列出无数据端口。主表不再显示累计收包和迟到包，保留累计缺包；`LOSS_PORTS`中的`3:6`表示端口3累计缺6包。`tlcache period`列出每个节点的周期范围及全部启用节点的共同可触发范围。累计计数仍保留在`tlcache --json`中供排障。管理查询端口先于大内存分配启动，因此准备内存期间也能查询，但不会接受触发。

多节点服务启停可在存储服务器、任意缓存节点或显控节点执行。目标支持`all`、逗号和范围；命令经142控制器并行调用各缓存节点上独立运行的root管理服务，不在运行时调用Ansible、SSH或sudo。管理服务只接受142控制器地址，控制器只接受存储节点、缓存节点和显控节点发起的启停命令。停止或重启会先拒绝涉及未完成事件的操作并要求一次确认；自动化可加`--yes`，只有人工确认可以丢弃活动事件时才加`--force`：

```bash
tlcache start all
tlcache stop 1-3
tlcache restart 2,5-7
tlcache stop all --yes
```

显控节点`172.0.0.61`也可查询全部节点。它在inventory中属于`display`组，由`deploy.sh`统一安装`tlcache`、`tltrigger`和节点清单，并由`update_software.sh`一并更新；部署不会修改显控节点网卡配置。

显控节点使用不同的sudo密码。运行统一部署或更新脚本时，先输入`tianlaiview`的sudo密码，再在Ansible提示中输入其他服务器的sudo密码；两种密码均不写入仓库。

脚本使用`ubuntu`账户并通过SSH交互认证，不保存密码。部署后在显控节点可执行`tlcache`、`tlcache --watch 2`、`tlcache period`以及缓存服务启停命令。

每个接收线程会打印第一条成功解析的UDP数据头，包括`packet_id`、板卡号、trace/integration周期和长度；随后再打印第一条实际写入环形缓冲区的数据。如果ixcap已经收到帧但以太网/IPv4/UDP解析失败，会打印首个失败帧的长度，便于区分“没有流量”和“帧格式不匹配”。

可在任意缓存节点或存储节点使用`tltrigger`；它已内置控制器地址，并自动生成请求编号和事件编号。先查询384路是否就绪：

```bash
tltrigger status --channels 384
```

控制器响应中的`node_runtime_states`和`runtime_state_counts`提供各节点运行阶段汇总。确认 `accept_trigger` 为 `true`，并确保所需起止周期位于返回的 `oldest_period` 与 `settled_end` 所示范围内；结束边界为不包含端点。然后提交触发：

```bash
tltrigger capture 3 --channels 384
```

上述命令自动查询共同可用范围，并保存其中最近3秒的数据；时长可以为1～50秒。`--channels`选择288或384，省略时默认384。若必须指定历史起点，可增加`--trace 120 --integration 45000`。首次回复`PREPARING`不表示已经写盘完成；复制返回的事件编号进行查询：

```bash
tltrigger event event-20261112-012345-abc123
```

需要恢复或取消暂停事件时使用`tltrigger resume EVENT_ID`或`tltrigger cancel EVENT_ID`。高级联调仍可使用`tltrigger --file request.json`发送完整协议JSON；完整字段与跨Trace格式见[协议说明](PROTOCOL.md)。

`tltrigger`由Ansible安装到控制器/存储节点`172.0.0.142`和显控节点`172.0.0.61`的`/usr/local/bin`。运行全量部署或软件更新后即可直接使用，不需要再执行独立分发脚本。

常见状态：`PREPARING`正在保护、`PREPARED`表示全体已保护、`STORING`正在双lane回传、`STORED`表示全体已持久化、`RELEASING`正在统一释放、`COMPLETED`表示全部完成。检查`missing_by_server`以及每台节点的`lane-0.json`和`lane-1.json`。

## 6. 故障与恢复

| 现象 | 检查和处理 |
| --- | --- |
| `BUSY_OR_WARMING` / 未就绪 | 检查是否有活动事件、积累时长、节点收包及控制器状态；`READY` 本身不能证明收包仍在持续。 |
| `EXPIRED` | 所请求的起点已经超出保留历史；检查触发延迟和周期范围。 |
| `NOT_READY` | 结束周期尚未定稿或尚未收到；当前实现不会等待未来数据。 |
| `PAUSED` / `SEND_FAILED` | 查看事件错误、各节点状态、网络和存储；按协议使用 `RESUME` 协调重试。若日志磁盘写入失败，先修复日志存储，重启后核对日志和各节点实际状态。节点已 `REJECTED` 的事件不能简单靠 `RESUME` 重新准备，须先明确处理/取消旧事件，再以新事件编号提交。 |
| 后端未收到完成通知 | 288路检查节点0～10共22个lane，384路检查0～13共28个lane，以及`event.json`中的`backend_acknowledged`、存储服务日志和后端TCP 19001。 |
| 有`.bin`但事件未完成 | 部分文件不能作为完成凭据；检查每节点两个lane标记和控制器状态。 |

控制器日志可用于重启后恢复状态，但缓存数据仍在节点内存，缓存进程退出或掉电后无法从内存恢复。取消事件会放弃相应受保护数据，应由现场操作人员根据事件状态决定。

## 7. 验收

在 Linux 项目根目录运行 `make test`，其中包含无 ixcap 的模拟构建；`make help` 可查看其他命令。模拟测试使用缩小缓存和本地数据，不代表真实环境的持续吞吐。正式投运前应核对：14 台接收映射和周期对齐、断流/丢包监控、跨缓冲触发、传输重试、掉电及故障恢复、通知与去重，以及真实网卡、NUMA、可用内存和磁盘阵列持续写入能力。测试结果应记录环境、版本、时长和缺包数量。
