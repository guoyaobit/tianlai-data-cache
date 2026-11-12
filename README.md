# 原始数据缓存：服务器 0～13

文档入口：[部署与联调操作手册](DEPLOYMENT_GUIDE.md) · [Ansible 部署](deploy/ansible/README.md) · [触发与存储协议](PROTOCOL.md) · [代码结构](CODE_STRUCTURE.md)。
首次部署请先阅读操作手册；本文提供架构和配置背景。所有 IP、端口和路径示例均须按现场实际情况修改。

前 11 台按 `tianlai-A288B288-20260917` 的输入包格式实现，新增 11～13 台接收远端子站数据。
沿用 GPU 程序的 C++14、
`YamlConfig`、`CacheRuntime`、`RecvAntenna`、`ReceiveRingBuffer`、`Prepare/Finish`、
线程入口和 `LOG` 组织方式；接收仍用 `ixcap`，不依赖 CUDA，不执行波束合成。

三类程序使用同一份 `Common/` 实现，避免触发/缓存逻辑分叉。部署或复制源码时须保留
`Common/` 与三个程序目录的相对位置。公共头文件在 `Common/include/`，实现位于 `Common/src/`，
`Common/.clang-format` 定义统一的 C++ 代码风格。原系统接收板卡 0～35；远端子站接收板卡 36～47，
每板 8 通道。新节点每板每周期有两个 FFT 半段包，缺包位图按包位置而非仅按板卡编号记录。

| 目录 | 节点 | 接收口 | 原 packetId/GPU ID | 每口板卡 | 每包原始数据 |
|---|---|---:|---|---|---:|
| `F0-2/` | 0～2 | 8 | `serverId*8+port` | 0～35 | 2304 B |
| `F3-10/` | 3～10 | 4 | `24+(serverId-3)*2+port/2` | 偶数口 0～17，奇数口 18～35 | 8832 B |
| `F11-13/` | 11～13 | 4 | `2*((serverId-11)*4+port)` 及下一个包号 | 每口 36～47，每板两包 | 8256 B |

每包保存完整 16 B 业务头及紧凑复数数据。Ethernet/IP/UDP 头不写盘。
远端子站每口 86 个频点，末口最后 **8** 个频点填零（86×12−1024=8），每板包号 **0～23**。
详细映射和容量见 [F11-13/README.md](F11-13/README.md)。

## 构建和启动

依赖Linux、g++、Boost头文件、yaml-cpp、libzmq和spdlog开发库（含其fmt依赖），以及缓存节点现有`ixcap_lib.h/libixcap.a`。C++存储服务另外需要C++17和nlohmann-json开发包。构建默认用`pkg-config`查询spdlog、libzmq和yaml-cpp；部署前在目标环境安装对应开发包并重新构建。

```bash
make -j4 production                  # 编译三类缓存程序和落盘服务
make -j4 production PROFILE=F3-10    # 仅编译一种类型
make -j4 storage                     # 仅编译 C++ 存储接收器
make -j4 simulation                  # 不使用 ixcap，构建全部模拟程序
```

以上命令从项目根目录执行。`make` 等同于 `make production`；使用 `make help` 查看入口。
可通过 `IXCAP_INCLUDE`、`IXCAP_LIB` 指定 ixcap 路径，例如
`make production PROFILE=F0-2 IXCAP_INCLUDE=/path/include IXCAP_LIB=/path/lib`。
模拟构建的程序名带 `-simulation` 后缀，仅用于 `--simulate` 回环联调。生产配置不包含 `simulationPortN`；回环联调时须为每个接收端口显式添加该参数。原来的各目录 `make` 入口仍可使用。
生产版和模拟版分别使用 `build/`、`build-simulation/`，不共用目标文件。

先修改每台对应的 `server_XX.yaml` 和该组的 `receive_config.yaml`：

- Ansible部署默认以 `ens2f0` 的Intel双口万兆卡型号为基准，排除该发送卡并自动发现其他同型号接收端口，端口可以有Linux网口或尚未加载驱动；手工运行时将 `deviceName0..7` 或 `deviceName0..3` 改为实际接收 PCI 地址。
- 填写单个存储网口对应的 `sendEthName`、本地IPv4、存储端IPv4及两个逻辑lane端口。
- `controlBindIp/controlPort` 是管理网 TCP/JSON 监听地址；`controlPeerIp` 只允许调度机地址。
- 节点间内部固定使用`acquisition_id=tianlai`；缓存YAML不再包含该固定项，消色散后端触发时也可省略该字段。
- `transitionWarnPercent` 默认 80；过渡区达到该占用比例时记录一次 WARN，并在查询结果中置警告标志。
- `chunkPeriods` 默认 64；它控制单个 ZeroMQ 数据块的积分周期数，应结合消息内存和吞吐实测调整。
- Ansible根据接收网卡NUMA拓扑自动填写`cpuIdN`并避开每个NUMA的首个物理核；手工配置时`-1`表示不绑定。内存预触页默认开启，仍需结合现场验证网卡、线程和缓存的本地性。
- `logFilePath` 是日志文件全路径；`-` 只输出到 stderr。`receive_config.yaml` 中可选的
  `debug: true` 启用详细诊断日志，省略或设为 `false` 时保持 INFO 级别。日志由 spdlog
  输出，带毫秒时间和线程号；指定文件时同时输出到 stderr，文件按 50 MiB 轮转，保留最多
  5 个备份。WARN 及以上立即刷新，其他日志按约 3 秒周期刷新；日志并非事件持久化凭据。
- 缓存程序以`Runtime state:`日志和`QUERY_STATUS.runtime_state`公开当前阶段；存储程序通过`/run/tianlai-storage-status.json`公开监听、接收写盘及后端通知状态。Ansible部署后可在任意节点运行`tlcache --watch 2`统一查看，或用`tlcache period`查询积分周期。

节点 YAML 的 `controlBindIp` 使用 `172.0.0.128～141`（server 0～13 对应 tianlaistore1～14），控制器/存储服务器管理地址为 `172.0.0.142`。状态查询和触发控制统一走该172管理网；消色散后端地址为 `192.168.200.10`，存储数据网地址为 `192.168.210.250`。当前配置统一使用 YAML。
程序在正式启动时拒绝未填写的 PCI 地址或发送接口。

```bash
cd F0-2
./tianlai-store-f0-2 server_00.yaml receive_config.yaml --check-config
sudo ./tianlai-store-f0-2 server_00.yaml receive_config.yaml
```

后 8 台在 `F3-10/` 运行 `tianlai-store-f3-10 server_03.yaml receive_config.yaml`，以此类推。
远端 3 台在 `F11-13/` 运行 `tianlai-store-f11-13 server_11.yaml receive_config.yaml`，以此类推。
`--check-config` 只检查结构和打印映射/计划内存，不分配大缓存、不初始化网卡；
它允许模板中的硬件占位符，不能代替硬件部署检查。

收包链路应支持原有大包，通常统一设置 MTU 9000。Ansible 完整部署会将速率大于
1 Gb/s 的接收侧物理 Linux 网口写入接收 Netplan 文件并设置为 MTU 9000。缓存节点发送口使用
单独的发送 Netplan 文件，固定为
`ens2f0`，server 0～13 自动配置 `192.168.210.100～113/24`，不设置默认网关；交换机对应端口仍需现场配置。
接收线程按帧解析 IPv4/UDP，支持
最多两层 VLAN；拒绝 IP 分片及错误长度。网卡入口校验和能力需要在实机确认。

## 内存和接收逻辑

默认每积分周期 200 us，每 trace 50000 周期：

- 每口 A/B 两个各 50 秒的缓冲；正常逐周期覆盖，合起来保留最近约 100 秒。
- 每台额外配置 32 GiB 过渡区，平均分到各接收口。
- 启动检查 `/proc/meminfo` 的 `MemAvailable`，另外预留 48 GiB 给系统/驱动等。
- F0-2 的 A/B 业务数据合计 334.080 GB；F3-10 为 318.528 GB。
  F11-13 为 397.056 GB；再加 32 GiB 过渡区和周期元数据，需按实际可用内存验证。
- 使用 `mmap`、大页提示、可配置预触页，不使用 CUDA pinned memory；每个端口在其接收 CPU 上完成缓存首次触页，使内存尽量位于网卡对应 NUMA 节点，运行期间应避免换页。
- 一个接收口一个写线程，批量取包；每周期保留板卡位图。缺包扫描仅在积分周期前进时执行，不对同周期的每个板卡包重复扫描。周期越过乱序窗口后仍缺包时
  输出 WARN，持续丢包按 1 秒窗口汇总，避免逐包日志影响接收性能。
- 默认容忍 50 个周期的乱序，断流 20 ms 后尾部可定稿；位图收齐也保留相同乱序窗口。
- 跳过的整周期仍通过时间范围和缺失位图表示，不能把丢包后的数据前移补齐。
- `ifTrace` 冲突、重复、迟到、错误包分别计数；已保护事件不受新数据覆盖。

`QUERY_STATUS` 返回当前最繁忙接收口的过渡区占用/容量、警告标志和最近冻结耗时，并提供进程启动以来的累计缺包、迟到包、最近缺包周期及按端口缺包统计；
`QUERY_PORT` 进一步返回单口峰值、冻结次数等指标。事件查询返回冻结耗时、发送耗时、
实测应用层发送速率及过渡区峰值，便于部署验收和定位过渡区不足。

收到合法触发，所有口先进入过渡接收，再后台将事件较少一侧复制到较多一侧。
同一事件最多 5 秒，按 `period % halfPeriods` 放置，原始大侧片段不移动。
两个 A/B 在复制期间均不可写，过渡区承接新包和切换时未定稿的尾部。
整理后，一个主缓冲区冻结用于发送，另一个按 50 秒循环接收。
过渡数据按周期/板卡合并，不覆盖已到达的新周期或重复板卡。
过渡区溢出会明确报错停机，不会静默覆盖事件并报告成功。

事件落盘确认后由调度器发送 RELEASE。节点保留当前实时缓冲，优先写已释放缓冲，
保留的周期编号跨度达到约 100 秒后才再次 READY；该状态不保证每个周期或包均已收到，也不证明当前仍在收包，需检查缺包与收包指标。
忙碌期间不接受第二个事件；不支持无限时间“开始录制/停止录制”。

## 管理网触发和调度

控制通道是千兆内网 **TCP + JSON**，每次连接处理一条请求和回复；每条消息使用4字节网络序长度前缀，JSON最多1200 B。
每次请求有 request_id，每个事件有 event_id；重传使用同一 ID。
节点通过事件状态保证幂等，同 ID 不同范围报冲突。进程内保存最近最多 64 个结束事件的
去重记录；外部必须始终使用唯一事件 ID，不能依赖无限期历史去重。

控制器等配套工具使用 Python 3.10+：

- `tools/controller.py`：外部TCP/JSON触发入口；288路调度节点0～10，384路调度节点0～13。
- `tools/trigger.py`：简化触发客户端，自动选择最近的共同缓存区，也兼容完整JSON请求。
- `Storage/tianlai-storage`：生产部署使用的 C++ ZeroMQ 接收与落盘服务。
- `Storage/storage.yaml`：C++ 落盘服务的完整示例配置；Ansible 会按现场清单生成正式配置。

生产部署中，控制器与 C++ 落盘服务运行在同一台存储服务器上，分别作为
`tianlai-controller.service` 和 `tianlai-storage.service`。控制器绑定管理网 IP，落盘服务绑定高速数据网 IP；两者不共用数据端口。

复制并修改 `tools/nodes_example.json` 的 14 台管理网地址，然后在调度机运行。
远端和原系统须使用同一积分计数起点；内部固定标识不会自动校正采集时钟偏差。

```bash
python3 tools/controller.py --nodes tools/nodes_example.json \
  --journal /data/store-control \
  --bind 172.0.0.142
```

控制器接受所有能够连接其管理网监听地址的触发来源，因此可从任意缓存节点、存储节点或消色散后端操作；节点 YAML 的 `controlPeerIp` 仍应当是调度机。
外部入口默认TCP 18999，节点默认TCP 19000。地址按现场替换。
控制器的 `--period-us` 和 `--integration-per-trace` 默认为 200 和 50000，必须与全部节点配置一致；变更节点采集参数时同时修改控制器参数。

部署后可在任意缓存节点或存储节点直接保存最近3秒的384路数据：

```bash
tltrigger capture 3
```

使用`tltrigger status --channels 288`查询288路状态，使用`tltrigger event EVENT_ID`查询写盘进度；高级联调仍支持`tltrigger --file tools/trigger_example.json`。

`tltrigger`不在Ansible管理范围内。在master节点单独执行`bash tools/deploy_tltrigger.sh`，默认只部署或更新`172.0.0.142`，无需编译、sudo或重启服务。

在触发JSON中设置`"channel_count":288`或`384`；省略时默认384。返回PREPARING表示请求正在处理；查询事件得到PREPARED/STORING后才表明所选模式全部节点已保护。控制器只有确认所有节点STORED后才统一RELEASE，单节点故障不会提前释放其他节点缓存。
QUERY_EVENT 查询进度，QUERY_STATUS 查询全系统是否 READY。
内部固定标识仍进入缓存事件和TLSTORE1元数据。升级时必须先停止触发并处理完旧事件，在维护窗口统一更新控制器、14台缓存服务和存储服务，不能混跑协议版本不同的进程。
所选模式的11台或14台节点持久化完成后，C++存储服务通过TCP/JSON直接向消色散后端发送
`CAPTURE_RESULT`；后端持久化结果后向报文来源地址回复 `ACK_RESULT`。未确认结果会落盘保存并在重启后继续重试。示例接收器为 `tools/result_listener.py`，协议和缺包含义见 `PROTOCOL.md`。默认要求配置全部
14台。调试时可通过Ansible的`tianlai_disabled_cache_nodes`明确排除离线节点；该模式不能表示288或384路全部完成。
原始输入头的 `ifTrace` 不充当触发位，也不修改保留字节。
协议字段和状态详见 [PROTOCOL.md](PROTOCOL.md)。

任何准备失败、TCP 确认不明或落盘故障会使调度事件 PAUSED，保留节点可能已保护的数据，
阻止新的独立事件。查询确认后使用 RESUME 继续，或明确 CANCEL 放弃。
调度器有磁盘日志，重启后未完成事件标为 PAUSED，等待 RESUME 查询节点实际状态再继续。
不要直接启动第二个调度器处理相同节点。全部节点返回 RELEASED 后事件 COMPLETED，
但下一次触发仍须等待 QUERY_STATUS 显示 READY。

## ZeroMQ 回传与存储

存储服务始终为14台服务器保留28个监听入口。288路事件只有节点0～10回传22个lane；384路事件由节点0～13回传28个lane。每个入口由一个线程顺序完成收包、校验和写盘，不再额外创建写盘线程；协议本身逐块确认，因此仍可在不同lane之间并行。每台缓存服务器只使用一个物理网口和一个源IP，两个基于TCP的ZeroMQ连接共享该网口；lane 0发送偶数接收口，lane 1发送奇数接收口，数据不复制。存储服务器使用一个100G物理口和一个IP。发送端要求libzmq支持ZMQ_BINDTODEVICE，且源IP必须属于指定接口。

发送端不设置应用层速率上限，两个lane会共同使用单个物理网口的可用带宽。实际吞吐由缓存服务器网卡、交换机、存储服务器100G网口和磁盘阵列中最慢的一环决定，必须实测持续写入能力和拥塞表现。

每条ZeroMQ消息由存储端检查并处理；数据块结构检查、写入页缓存后返回OK，再发送下一块。存储端累计256 MiB或经过1秒后批量`fdatasync`，收到END后强制同步全部数据文件，再持久化完成标记并返回STORED。块级OK只表示结构检查通过并已写入，不表示断电安全；节点只有验证最终STORED后才允许释放事件。连接、超时或写盘失败时节点保留冻结数据并报告SEND_FAILED。

缓存到存储链路不计算应用层CRC，40字节块头的末4字节为0。传输可靠性依赖TCP校验与重传；存储端仍校验消息长度、周期范围、端口映射和原始包头，但不检测内存、PCIe或磁盘静默位翻转。

在存储服务器编译并运行 C++ 接收器（地址按现场替换）：

```bash
make storage
./Storage/tianlai-storage --config Storage/storage.yaml --check-config
./Storage/tianlai-storage --config Storage/storage.yaml
```

`sendDestinationIp`指向100G口上的单个IP，两个lane端口分别为`20000 + server_id`和`20100 + server_id`。监听项同时校验server、lane和来源IP。每个lane只使用一个`lane-N.json`保存元数据、接收状态和缺包统计；最终确认丢失时，已完成lane在元数据阶段直接返回STORED。生产模式下288路需要22个lane、384路需要28个lane持久化状态一致，才生成事件根目录的`event.json`并通知后端；同一文件的`backend_acknowledged`记录后端是否确认。调试排除离线节点时，控制器与落盘端使用同一启用节点列表，完成通知明确标记`partial_node_set`及实际参与节点。

ZeroMQ 数据通道不使用 UDP 分片参数；旧配置中的 udpFragmentBytes 必须删除。TCP/IP MTU 仍须按实际网络配置。ZeroMQ 不会补回 FPGA 到缓存服务器之间丢失的 UDP 包；旧 UDP/TCP 存储发送端与 ZeroMQ 接收器不兼容，需协调同时更新。

程序不主动限速，因此不能把网口线速当作已验证的持续落盘能力。默认线程结构为1个主线程、28个lane线程、2个ZeroMQ I/O线程和1个完成通知线程，应用可见合计约32个，不再动态增加28个写盘线程；libzmq可能另有内部管理线程。100G网卡RSS、中断亲和性、NUMA、交换机拥塞和磁盘持续写入仍须实机验收；若2个I/O线程成为瓶颈，可实测后提高`ioThreads`。

## 缓存进程异常退出与恢复

缓存数据只存在内存中，进程退出、服务器重启或掉电都会丢失该节点尚未完成的滚动缓存和冻结事件，程序不会伪造恢复成功。重启后的节点从空缓存开始，必须重新接收约 100 秒数据，`QUERY_STATUS` 返回 READY 后才能接受新事件。控制器重启日志中的未完成事件应保持 PAUSED；运维人员需查询14台节点，如果任一节点已经重启或找不到原事件，应取消该事件并使用新的 `event_id`，不得直接 RELEASE 或将部分节点结果标记为完整。已经取得最终 STORED 的事件可由存储端持久化标记幂等确认，不需要重新发送。

## 验证

```bash
make test
```

`make test`通过统一入口构建模拟版后运行回归和联调；可通过`PYTHON`、`CXX`指定解释器和编译器，例如`make test PYTHON=python3 CXX=g++`。测试仍需Boost、yaml-cpp、spdlog和支持ASan/UBSan的编译器，不需要ixcap。`make clean`会删除三个类型的构建目录、可执行文件及落盘服务构建产物；`make clean PROFILE=F3-10`只清理指定类型，运行前请确认不需要保留相关产物。

测试另需系统 Python 的 pyzmq、libzmq 和 nlohmann-json 开发库。测试使用 ASan/UBSan 检查真实缓冲实现，覆盖跨 A/B、非零起始周期、循环覆盖、缺包、
整周期缺失、并发收包、过渡溢出、冻结及恢复。回环联调运行三类真实程序的 `--simulate`，
验证UDP数据输入、TCP/JSON控制、重复触发、ZeroMQ发送失败保留与重试、最终确认丢失、逐字节落盘内容和调度流程，
并检查全部 14 份节点映射；新模式额外验证同板奇偶包独立存储及末口填充频点。
测试使用临时目录和缩小的缓存，不访问实际采集网卡。

测试不替代 512 GB 内存、实际 ixcap 网卡、多口持续吞吐、NUMA、真实时钟和磁盘阵列验收。
