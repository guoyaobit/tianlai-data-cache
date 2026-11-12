# Ansible 部署

`site.yml` 在 Debian/Ubuntu 系的systemd主机上部署14台缓存节点、同机运行的存储接收器/控制器及显控节点管理客户端；不部署消色散后端，也不配置交换机、管理网地址、存储服务器100G地址、磁盘阵列或ixcap。它会通过Netplan配置缓存节点`ens2f0`数据地址，并把缓存/存储节点上速率大于1 Gb/s的物理Linux网口设置为MTU 9000；不会修改显控节点网络配置。此脚本尚需在目标环境进行端到端验证。

## 前提和清单

- 控制机安装 Ansible，能通过 SSH 连接目标主机并提权；目标主机须能安装 Debian 软件包，控制器须有 Python 3.10 以上。脚本为缓存节点和存储服务器安装 libzmq 开发包，并为 C++ 存储服务安装 yaml-cpp、nlohmann-json 和编译工具。
- 生产版缓存程序需要 `ixcap_lib.h` 和 `libixcap.a` 或 `libixcap.so`。部署优先检查清单中的 `tianlai_ixcap_include`、`tianlai_ixcap_lib`，找不到时自动搜索 `/usr/local`、`/usr`、`/opt` 和部署账户主目录。源码和配置会先复制，再执行SDK检查和编译；SDK确实不存在时源码仍保留在 `/opt/tianlai-store/src`，但生产程序无法编译。
- ixcap尚未安装时，在Ansible master准备三个已解压的SDK文件和许可证TAR包，然后运行 [`deploy/ansible/deploy_ixcap.sh`](deploy_ixcap.sh) 统一安装SDK、MAC许可证和systemd大页服务；无需解压或指定SDK ZIP。详细说明见 [`deploy/ixcap/README.md`](../ixcap/README.md)。
- 真实地址、链路、网卡 PCI 地址、光模块、驱动、NUMA、交换机 Jumbo Frame 及存储挂载由现场先完成验证。`tianlai_storage_directory` 必须指向目标机已有且正确挂载的存储路径下的目录；脚本只创建目录，不验证其所在磁盘或容量。
- 清单可使用 YAML 或 INI：复制 `inventory_example.yml` 为 `inventory.yml`，或复制 `inventory_example.ini` 为 `inventory.ini`。两份示例都包含缓存服务器1～14（节点0～13）、同机部署的存储/控制器，以及`172.0.0.61`显控节点；选择一种维护即可。须现场核对SSH地址。`tianlai_receive_devices: auto`以`ens2f0`所在Intel双口万兆卡的PCI厂商/设备ID为基准，排除该发送卡的整个PCI插槽，再选择其他同型号端口；候选端口允许存在Linux网口，也允许尚未加载驱动。节点1～3必须发现8个，固定顺序为`ens4f0、ens4f1、ens5f0、ens5f1、ens1f0、ens1f1、ens3f0、ens3f1`；节点4～11必须发现4个，固定顺序为`ens4f0、ens4f1、ens5f0、ens5f1`。部署会把名称解析成PCI地址，并校验名称集合与自动发现结果完全一致。节点12～14暂时继续按PCI地址排序。也可把`auto`替换为按端口顺序排列的PCI地址列表。缓存节点发送口统一为`ens2f0`，地址根据server ID自动生成，无需逐台填写。`controller`与`storage`组必须使用同一个存储服务器主机名。
- 缓存节点单个发送接口/源IP由 server ID 自动生成；清单只需填写存储100G口上的单个无标签IP。存储服务器固定使用 `enp152s0f0np0` 接收14台缓存节点的数据。不配置VLAN，数据接口不设置默认网关；管理网仍使用`tianlai_controller_ip`。
- 数据网口不要求物理链路处于UP状态。部署会为缓存发送口、存储100G口和已识别的高速数据口设置NetworkManager `ignore-carrier`，因此光纤暂未连接时仍可写入静态IP、MTU和代码。管理网SSH必须可达，否则Ansible无法向该节点传输文件；恢复后可用 `--limit` 单独重跑失败节点。
- 消色散后端须在`tianlai_backend_ip:tianlai_notify_port`监听TCP/JSON。存储服务从`tianlai_controller_ip`连接并发送`CAPTURE_RESULT`；后端持久化结果后在同一连接回复`ACK_RESULT`。Playbook不部署后端，也不修改相关防火墙。
- 管理网需放通缓存节点、存储节点和显控节点→控制器TCP 18999、控制器→缓存节点TCP 19000、存储服务器→后端TCP 19001；实际端口以清单为准。
- 当前规划中状态查询和触发控制统一使用管理网：缓存节点为 `172.0.0.128～141`，控制器/存储服务器为 `172.0.0.142`。触发控制器接受所有能够连接其管理地址的来源，可从任意缓存节点、存储节点或消色散后端发送命令。消色散后端地址为 `192.168.200.10`，存储数据地址为 `192.168.210.250`，默认落盘根目录为 `/data`；部署前确认这些地址和挂载点与现场一致。
- 显控节点`172.0.0.61`作为inventory中的`display`角色由Ansible管理。`deploy.sh`和`update_software.sh`会同时更新控制器及显控节点上的`tlcache`、`tltrigger`；显控节点不参与Netplan业务网配置。
- 显控节点的sudo密码与服务器不同时，`deploy.sh`和`update_software.sh`会先单独提示输入显控节点密码，再由Ansible提示输入其他服务器的密码。密码只保存在当前脚本进程的环境变量中，不写入inventory或命令行。无人值守执行前可自行设置`TIANLAI_DISPLAY_BECOME_PASSWORD`。
- 如果目标上安装的 ixcap 是共享库，确保运行时动态链接器可找到该库。脚本只检查文件存在，不负责驱动、权限或网卡初始化。

## SSH登录和sudo密码

所有目标服务器使用`ubuntu`账户。部署脚本不指定私钥路径，直接使用系统默认SSH配置、默认密钥或已加载到`ssh-agent`中的密钥。已经能够免密登录时不需要执行其他SSH准备操作。新服务器尚未配置免密登录时，可使用系统默认密钥执行：

```bash
ssh-copy-id ubuntu@SERVER_IP
```

该命令对14台缓存服务器、存储服务器和显控节点各执行一次，首次执行时使用服务器现有密码。完成后用`ssh ubuntu@SERVER_IP`确认可以免密登录，再继续部署。

部署不保存SSH或sudo密码。直接运行一键部署脚本，先输入显控节点的sudo密码，再输入其他服务器共用的sudo密码：

```bash
bash deploy.sh
```

`deploy.sh`先检查清单和语法，再在本次选择的实际目标主机变量上下文中执行一次完整性预检。执行顺序为：必要网络配置、存储/缓存/控制器代码复制与编译安装、最后生成网卡报告；报告问题不会阻止前面的代码部署。可附加Ansible参数，例如`bash deploy.sh --limit tianlaistore1`。默认读取`inventory.yml`，使用INI清单时执行`TIANLAI_INVENTORY=inventory.ini bash deploy.sh`。`ansible.cfg`启用15路并发和SSH pipelining，减少批量部署连接次数。节点内部固定使用`acquisition_id=tianlai`，不再写入缓存YAML。部署完成后存储和控制器自动启动；`tianlai-cache.service`保持禁用和停止，并将配置的接收PCI设备逐个恢复到ixgbe驱动。

调试时可在`inventory.yml`的`all.vars`中明确列出离线缓存节点：

```yaml
tianlai_disabled_cache_nodes: [tianlaistore5, tianlaistore9]
```

控制器节点清单和落盘端等待列表都会排除这些主机，并自动启用部分节点模式。至少保留一个缓存节点，名称必须属于`cache_nodes`且不能重复。此模式生成的数据不完整，不得作为288或384路完整结果；恢复生产前必须清空该列表并重新部署存储服务器上的控制器和落盘服务。Ansible仍会尝试部署离线主机，现场可配合`--limit`只更新在线节点及存储服务器。

部署会读取每个接收PCI设备的NUMA节点，并为接收线程选择同NUMA的不同物理CPU核；默认每个NUMA节点预留第一个物理核。最终`cpuIdN`写入`server.yaml`，NUMA未知或可用物理核不足时部署停止。预留数量由`tianlai_receive_reserved_cores_per_numa`配置。

首次完整部署完成后，可只更新项目源代码和程序，不修改Netplan、YAML业务配置、采集任务ID、systemd单元或系统软件包：

```bash
bash update_software.sh
```

该命令更新并编译缓存程序和C++落盘程序，同时更新Python控制器，以及控制器/显控节点上的`tlcache`、`tltrigger`。更新前正在运行的服务会在程序替换后恢复运行；原本停止的服务保持停止。先更新单台缓存服务器可执行 `bash update_software.sh --limit tianlaistore1`；只更新显控节点可使用`--limit display`，只更新存储/控制器可使用 `--limit storage` 或 `--limit controller`。使用INI清单时执行 `TIANLAI_INVENTORY=inventory.ini bash update_software.sh`。

主部署会在所有缓存节点、存储服务器、控制器和显控节点安装网络诊断工具，包括 `bwm-ng`、`ethtool`、`iperf3`、`tcpdump`、完整的`wireshark`、命令行工具`tshark`、`mtr`、`nmap`、`iftop` 和 `nload`。`tshark`和`dumpcap`默认要求sudo权限。独立Playbook只安装或更新这组工具，不执行系统全量升级，也不修改网络和业务配置：

```bash
ansible-playbook -i inventory.yml --ask-become-pass install_network_tools.yml
```

重复执行同一命令即可将这组工具更新到当前软件源提供的最新版本。只更新单台服务器可追加 `--limit tianlaistore1`；使用INI清单时将 `inventory.yml` 替换为 `inventory.ini`。

## 部署前盘点网卡

`inspect_nics.yml` 可以在完整部署配置准备好之前单独运行。所有目标服务器默认使用 `ubuntu` 账户和系统SSH配置登录；请核对要连接的服务器的 `ansible_host`，缓存节点的 `tianlai_server_id` 使用示例中的编号。以下命令以 YAML 清单为例，使用 INI 时将 `inventory.yml` 替换为 `inventory.ini`。控制机需要 Ansible 和 SSH 连接，目标服务器需要 `/usr/bin/python3`；安装 `iproute2` 后才能列出接口 IP。盘点不需要 sudo，不修改网络配置、驱动或服务。Ansible 会在目标机临时传送并执行盘点脚本。

从本目录执行：

```bash
# 先盘点一台；可以替换成其他清单中的主机名
ansible-playbook -i inventory.yml inspect_nics.yml --limit tianlaistore1

# 盘点所有缓存服务器
ansible-playbook -i inventory.yml inspect_nics.yml --limit cache_nodes

# 填好所有主机的 SSH 地址后，盘点缓存服务器及兼任控制器的存储服务器
ansible-playbook -i inventory.yml inspect_nics.yml
```

盘点结束后会在控制机生成 `reports/network-inventory.md`，使用统一列表汇总成功完成盘点的服务器；失败、不可达或未生成盘点事实的主机会列在 `Missing inventory` 中，不会导致Markdown生成任务再次失败。文档包含
配置、接口、IP、MAC、链路速率、PCI 地址、驱动、NUMA 信息及告警。使用 `--limit` 时，文档只包含本次实际盘点的主机；
再次执行会覆盖旧报告。该目录已被 Git 忽略，避免现场网络信息被提交到远程仓库。

示例清单中的控制器地址是占位符；运行全部主机前须填写实际地址，也可以用 `--limit` 只选择已准备好的主机。盘点输出中各项含义：

| 输出项 | 用途 |
| --- | --- |
| `expected_cache_receive_ports` | 缓存服务器 1～3 各需 8 个接收端口，4～14 各需 4 个；其他角色不适用。 |
| `expected_cache_send_interfaces` | 每个缓存节点使用1个存储数据发送接口。 |
| `configured_receive_devices`、`configured_send_interface`、`configured_send_ip`、`configured_control_ip` | 显示清单中已有值，便于与现场盘点结果核对。 |
| `interfaces` | 系统网络接口的名称、MAC、IP、MTU、状态、链路速率、PCI 地址、驱动和 NUMA 节点。 |
| `pci_network_devices` | PCI 网络设备的地址、厂商和设备编号、驱动、NUMA 节点及其系统接口；没有系统接口的设备也会列出。 |
| `warnings` | 无法读取 IP 或系统设备信息时的提示。 |

现场确认物理接线后，默认由`tianlai_receive_devices: auto`发现接收PCI地址；这些地址不是`ens2f0`一类Linux网口名。现场结构中缓存1～3为5张双口万兆卡、缓存4～14为3张，端口5/6所在卡用于发送；程序通过`ens2f0`的PCI插槽排除整张发送卡。缓存1～3采用8口固定顺序，缓存4～11采用4口固定顺序，缓存12～14暂按PCI地址排序。缓存节点发送口固定为`ens2f0`；server ID 0～13自动映射为`192.168.210.100～113/24`。存储服务器固定使用`enp152s0f0np0`和`192.168.210.250/24`；`inventory.yml`中的`tianlai_storage_ip`必须为有效IPv4地址，即使使用`--limit`，部署也会在写入网络配置前检查该地址。单独执行`inspect_nics.yml`只盘点、不修改；完整执行`site.yml`或`deploy.sh`会先强制停止缓存服务并把接收PCI设备恢复到ixgbe，再生成三类独立配置：`/etc/netplan/98-tianlai-receive.yaml`管理接收PCI对应的Linux网口，设置MTU 9000并启用混杂模式；缓存节点的`/etc/netplan/99-tianlai-send.yaml`管理`ens2f0`，存储服务器的`/etc/netplan/99-tianlai-storage.yaml`管理`enp152s0f0np0`。混杂模式由网络部署命令设置，并由NetworkManager dispatcher在接收网卡重新出现时恢复，不在缓存程序中设置。每个受管接口都显式使用NetworkManager渲染器。两个数据口均使用静态IPv4地址、MTU 9000且不添加默认路由；部署流程不配置或检查IPv6，也不检查其他接口上的`169.254.0.0/16`地址。部署会把现有Netplan YAML文件权限统一收紧为`0600`。配置先经过`netplan generate`，再由`netplan apply`生效，重启后仍然有效。其他1 GbE、回环、虚拟接口和速率未知接口不会修改。

交换机对应端口和链路中间设备也必须支持 MTU 9000，否则大包可能被丢弃。部署后可核对：

```bash
# Only configure cache data IPs and MTU.
ansible-playbook -i inventory.yml configure_network.yml

ip -br link
sudo cat /etc/netplan/98-tianlai-receive.yaml
sudo cat /etc/netplan/99-tianlai-send.yaml
sudo cat /etc/netplan/99-tianlai-storage.yaml
sudo netplan status --all
```

## 执行

从本目录执行（以下以 YAML 清单为例；INI 清单把文件名改为 `inventory.ini`。切勿提交包含现场地址、凭据或私钥的清单）：

```bash
# Recommended: validates configuration and then deploys.
bash deploy.sh

# Equivalent manual commands.
ansible-playbook -i inventory.yml --syntax-check site.yml
ansible-playbook -i inventory.yml --ask-become-pass site.yml

```

完整清单通过校验后，部署会自动在每台缓存服务器、兼任控制器的存储服务器及显控节点生成 `{{ tianlai_root }}/config/network-report.yaml`（默认 `/opt/tianlai-store/config/network-report.yaml`），同时在控制机更新 `reports/network-inventory.md`，并在存储服务器生成 `/opt/tianlai-store/config/storage.yaml`。重复部署时更新。可在对应服务器上查看：

```bash
sudo cat /opt/tianlai-store/config/network-report.yaml
```

报告中的`matched_receive_devices`和`matched_send_interfaces`将配置与现场设备对应；任一发送接口为`null`表示未找到。`speed_mbps=10000`表示当前协商为10 Gbit/s，未连接时可能为`null`。

报告仅记录配置和观察结果，不会自动选择物理端口、设置系统 IP 或修改驱动。部署前仍需按上一节完成端口对应关系和清单填写；如果完整清单校验未通过，部署及自动报告生成均不会开始，可先单独运行 `inspect_nics.yml` 盘点。

SSH登录使用系统默认配置，sudo密码仅在本次部署进程内使用，不写入磁盘。`/opt/tianlai-store` 默认保持 `root` 所有权，组设为部署账户 `ubuntu`；目录和程序允许 `ubuntu` 读取/执行，但只有 root 可以修改，避免普通用户篡改由 root 服务运行的程序。部署会安装 apt 软件包、复制源码及工具，通过项目统一的`make production PROFILE=...`入口编译缓存节点、生成YAML和控制器节点清单。缓存服务默认不启动；缓存服务正常停止或异常退出后，`ExecStopPost`都会只对配置中的接收PCI设备恢复ixgbe，不全局卸载ixgbe，也不重复绑定已经使用ixgbe的设备，因此不会影响发送口和其他Intel网口。`systemctl stop`直接向主进程发送`SIGKILL`，不等待线程退出，然后执行网卡恢复。若服务设置为失败后自动重启，设备会先恢复到ixgbe，再由下一次ixcap初始化接管。**停止缓存服务会立即丢失全部内存缓存、尚未完成的冻结事件和正在发送的数据**；应先确认没有活动或暂停事件，并安排维护窗口。启动后必须等待约100秒预热，并始终使用全局唯一的新`event_id`触发。

服务默认使用 root，节点通过 ixcap 收包；脚本没有配置防火墙或服务用户隔离，需结合现场权限和网络策略审查。软件包安装、服务启停和数据保存路径均由执行 playbook 的操作者负责确认。代码部署完成后确认服务状态，需要采集时再启动缓存服务：

```bash
systemctl status tianlai-storage.service
systemctl status tianlai-cache.service
systemctl status tianlai-controller.service
```

存储服务器、全部缓存节点和显控节点都会安装`tlcache`命令。它通过172管理网同时显示142存储服务状态，以及全部缓存节点的运行阶段、接收口、事件、累计收包、缺包、迟到包及按端口缺包统计：

```bash
tlcache
tlcache --watch 2
tlcache period
```

节点管理查询端口先于大内存分配启动，所以汇总工具能显示`PREPARING_MEMORY`；也可在相应节点执行`journalctl -u tianlai-cache.service -f`查看完整状态切换日志。

从Ansible master启动全部缓存节点：

```bash
ansible cache_nodes -i inventory.yml --become --ask-become-pass \
  -m systemd -a "name=tianlai-cache.service state=started"
```

分别在对应机器执行上述命令，并按[部署与联调手册](../../DEPLOYMENT_GUIDE.md)检查 14 台收包、计数同步、触发、落盘、缺包、通知和真实吞吐。服务启动不等于缓存已 READY，也不表示真实设备验收通过。

每节点不做应用层限速，两个逻辑lane共享一个物理网口。存储服务为14台节点生成28个监听端点：lane 0使用20000～20013，lane 1使用20100～20113。每个端点线程同时负责协议和写盘，默认使用2个ZeroMQ I/O线程，应用可见线程数约32个，libzmq可能另有内部管理线程。修改端口基数时应同时更新节点与存储服务。
