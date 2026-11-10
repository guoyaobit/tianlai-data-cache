# Ansible 部署

`site.yml` 在 Debian/Ubuntu 系的 systemd 主机上部署14台缓存节点，并在同一台存储服务器上部署存储接收器和控制器；不部署消色散后端，也不配置交换机、管理网地址、存储服务器100G地址、磁盘阵列或 ixcap。它会通过 Netplan 配置缓存节点 `ens2f0` 数据地址，并把速率大于 1 Gb/s 的物理 Linux 网口设置为 MTU 9000。此脚本尚需在目标环境进行端到端验证。

## 前提和清单

- 控制机安装 Ansible，能通过 SSH 连接目标主机并提权；目标主机须能安装 Debian 软件包，控制器须有 Python 3.10 以上。脚本为缓存节点和存储服务器安装 libzmq 开发包，并为 C++ 存储服务安装 yaml-cpp、nlohmann-json 和编译工具。
- 每个缓存节点事先安装对应的 `ixcap_lib.h` 和 `libixcap.a` 或 `libixcap.so`；默认在 `/usr/local/include`、`/usr/local/lib`，可在清单覆盖 `tianlai_ixcap_include`、`tianlai_ixcap_lib`。
- 真实地址、链路、网卡 PCI 地址、光模块、驱动、NUMA、交换机 Jumbo Frame 及存储挂载由现场先完成验证。`tianlai_storage_directory` 必须指向目标机已有且正确挂载的存储路径下的目录；脚本只创建目录，不验证其所在磁盘或容量。
- 清单可使用 YAML 或 INI：复制 `inventory_example.yml` 为 `inventory.yml`，或复制 `inventory_example.ini` 为 `inventory.ini`。两份示例都包含缓存服务器 1～14（节点 0～13）及同机部署的存储/控制器；选择一种维护即可。须现场核对 SSH 地址并补齐各节点的接收 PCI 地址。缓存节点发送口统一为 `ens2f0`，地址根据 server ID 自动生成，无需逐台填写。`controller` 与 `storage` 组必须使用同一个存储服务器主机名。
- 缓存节点单个发送接口/源IP由 server ID 自动生成；清单只需填写存储100G口上的单个无标签IP。存储服务器固定使用 `enp152s0f0np0` 接收14台缓存节点的数据。不配置VLAN，数据接口不设置默认网关；管理网仍使用`tianlai_controller_ip`。
- 消色散后端须在`tianlai_backend_ip:tianlai_notify_port`监听TCP/JSON。存储服务从`tianlai_controller_ip`连接并发送`CAPTURE_RESULT`；后端持久化结果后在同一连接回复`ACK_RESULT`。Playbook不部署后端，也不修改相关防火墙。
- 管理网需放通后端→控制器TCP 18999、控制器→缓存节点TCP 19000、存储服务器→后端TCP 19001；实际端口以清单为准。
- 如果目标上安装的 ixcap 是共享库，确保运行时动态链接器可找到该库。脚本只检查文件存在，不负责驱动、权限或网卡初始化。

## SSH密钥和sudo密码

所有目标服务器使用`ubuntu`账户。控制机默认使用`~/.ssh/tianlai_ansible`私钥；需要使用其他路径时设置环境变量`TIANLAI_SSH_PRIVATE_KEY`。不要把私钥放进项目目录。SSH公钥已经安装时无需再次执行`ssh-copy-id`。新服务器首次准备方法为：

```bash
umask 077
ssh-keygen -t ed25519 -f ~/.ssh/tianlai_ansible -C tianlai-ansible
ssh-copy-id -i ~/.ssh/tianlai_ansible.pub ubuntu@SERVER_IP
```

第二条命令对14台缓存服务器和存储服务器各执行一次，首次执行时使用服务器现有密码。完成后用`ssh -i ~/.ssh/tianlai_ansible ubuntu@SERVER_IP`确认可以免密登录，再继续部署。

部署不保存SSH或sudo密码。直接运行一键部署脚本，按提示输入一次所有服务器共用的sudo密码：

```bash
bash deploy.sh
```

`deploy.sh`先检查清单和语法，再执行完整部署；可附加Ansible参数，例如`bash deploy.sh --limit tianlaistore1`。默认读取`inventory.yml`，使用INI清单时执行`TIANLAI_INVENTORY=inventory.ini bash deploy.sh`。`ansible.cfg`启用15路并发和SSH pipelining，减少批量部署连接次数。部署阶段不需要填写采集任务 ID：存储服务会启动，缓存服务和控制器会安装但保持停止，防止使用错误任务 ID 接收触发。

## 部署前盘点网卡

`inspect_nics.yml` 可以在完整部署配置准备好之前单独运行。所有目标服务器默认使用 `ubuntu` 账户和上述SSH密钥登录；请核对要连接的服务器的 `ansible_host`，缓存节点的 `tianlai_server_id` 使用示例中的编号。以下命令以 YAML 清单为例，使用 INI 时将 `inventory.yml` 替换为 `inventory.ini`。控制机需要 Ansible 和 SSH 连接，目标服务器需要 `/usr/bin/python3`；安装 `iproute2` 后才能列出接口 IP。盘点不需要 sudo，不修改网络配置、驱动或服务。Ansible 会在目标机临时传送并执行盘点脚本。

从本目录执行：

```bash
# 先盘点一台；可以替换成其他清单中的主机名
ansible-playbook -i inventory.yml inspect_nics.yml --limit tianlaistore1

# 盘点所有缓存服务器
ansible-playbook -i inventory.yml inspect_nics.yml --limit cache_nodes

# 填好所有主机的 SSH 地址后，盘点缓存服务器及兼任控制器的存储服务器
ansible-playbook -i inventory.yml inspect_nics.yml
```

盘点结束后会在控制机生成 `reports/network-inventory.md`，使用统一列表汇总所有服务器的
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

现场确认物理接线后，将接收PCI地址填入`tianlai_receive_devices`。缓存节点发送口固定为 `ens2f0`；server ID 0～13 自动映射为 `192.168.210.100～113/24`。存储服务器固定使用 `enp152s0f0np0` 和 `192.168.210.250/24`；`inventory.yml` 中的 `tianlai_storage_ip` 必须为有效IPv4地址，即使使用 `--limit`，部署也会在写入网络配置前检查该地址。单独执行 `inspect_nics.yml` 只盘点、不修改；完整执行 `site.yml` 或 `deploy.sh` 会生成三类独立配置：`/etc/netplan/98-tianlai-receive.yaml` 管理检测到的其他高速物理网口及其 MTU，缓存节点的 `/etc/netplan/99-tianlai-send.yaml` 管理 `ens2f0`，存储服务器的 `/etc/netplan/99-tianlai-storage.yaml` 管理 `enp152s0f0np0`。每个受管接口都显式使用 NetworkManager 渲染器。两个数据口均使用静态IPv4地址、MTU 9000且不添加默认路由；部署流程不配置或检查 IPv6，也不检查其他接口上的 `169.254.0.0/16` 地址。部署会把现有 Netplan YAML 文件权限统一收紧为 `0600`。配置先经过 `netplan generate`，再由 `netplan apply` 生效，重启后仍然有效。其他1 GbE、回环、虚拟接口和速率未知接口不会修改。绑定 ixcap 专用驱动且未呈现为 Linux netdev 的接收卡不受 Netplan 管理。

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

# Set the acquisition ID, then start all cache nodes and the controller.
bash configure_acquisition.sh tianlai-20261109-run01
```

`configure_acquisition.sh` 是观测任务配置命令。参数只能包含 1～64 个字母、数字、下划线或连字符。它会把同一个 `acquisitionId` 写入全部14台缓存节点和控制器，校验缓存配置，然后统一启用并重启缓存服务和控制器；存储服务无需重启。切换采集任务时再次执行该命令即可，无需重新部署代码。使用 INI 清单时执行：

```bash
TIANLAI_INVENTORY=inventory.ini bash configure_acquisition.sh tianlai-20261109-run01
```

完整清单通过校验后，部署会自动在每台缓存服务器和兼任控制器的存储服务器生成 `{{ tianlai_root }}/config/network-report.yaml`（默认 `/opt/tianlai-store/config/network-report.yaml`），同时在控制机更新 `reports/network-inventory.md`，并在存储服务器生成 `/opt/tianlai-store/config/storage.yaml`。重复部署时更新。可在对应服务器上查看：

```bash
sudo cat /opt/tianlai-store/config/network-report.yaml
```

报告中的`matched_receive_devices`和`matched_send_interfaces`将配置与现场设备对应；任一发送接口为`null`表示未找到。`speed_mbps=10000`表示当前协商为10 Gbit/s，未连接时可能为`null`。

报告仅记录配置和观察结果，不会自动选择物理端口、设置系统 IP 或修改驱动。部署前仍需按上一节完成端口对应关系和清单填写；如果完整清单校验未通过，部署及自动报告生成均不会开始，可先单独运行 `inspect_nics.yml` 盘点。

SSH登录使用私钥，sudo密码仅在本次部署进程内使用，不写入磁盘。部署会安装 apt 软件包、复制源码及工具，通过项目统一的`make production PROFILE=...`入口编译缓存节点、生成YAML和控制器节点清单；存储服务器上的`tianlai-storage`会启动，缓存节点`tianlai-cache`和存储服务器上的`tianlai-controller`须由`configure_acquisition.sh`写入任务 ID 后启动。重跑部署会停止缓存服务和控制器。**停止或重启缓存服务会丢失全部内存缓存及尚未完成的冻结事件**；应先确认没有活动或暂停事件，并安排维护窗口。重新执行任务配置命令后必须等待约100秒预热，使用新的`event_id`触发。

服务默认使用 root，节点通过 ixcap 收包；脚本没有配置防火墙或服务用户隔离，需结合现场权限和网络策略审查。软件包安装、服务启停和数据保存路径均由执行 playbook 的操作者负责确认。代码部署完成后先确认存储服务；执行任务配置命令后再确认缓存服务和控制器：

```bash
systemctl status tianlai-storage.service
systemctl status tianlai-cache.service
systemctl status tianlai-controller.service
```

分别在对应机器执行上述命令，并按[部署与联调手册](../../部署与联调手册.md)检查 14 台收包、计数同步、触发、落盘、缺包、通知和真实吞吐。服务启动不等于缓存已 READY，也不表示真实设备验收通过。

每节点不做应用层限速，两个逻辑lane共享一个物理网口。存储服务为14台节点生成28个监听端点：lane 0使用20000～20013，lane 1使用20100～20113。每个端点线程同时负责协议和写盘，默认使用2个ZeroMQ I/O线程，应用可见线程数约32个，libzmq可能另有内部管理线程。修改端口基数时应同时更新节点与存储服务。
