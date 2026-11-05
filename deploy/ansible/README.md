# Ansible 部署

`site.yml` 在 Debian/Ubuntu 系的 systemd 主机上部署14台缓存节点，并在同一台存储服务器上部署存储接收器和控制器；不部署消色散后端，也不配置交换机、网卡地址、MTU、磁盘阵列或 ixcap。此脚本尚需在目标环境进行端到端验证。

## 前提和清单

- 控制机安装 Ansible，能通过 SSH 连接目标主机并提权；目标主机须能安装 Debian 软件包，控制器须有 Python 3.10 以上。脚本为缓存节点和存储服务器安装 libzmq 开发包，并为 C++ 存储服务安装 yaml-cpp、nlohmann-json 和编译工具。
- 每个缓存节点事先安装对应的 `ixcap_lib.h` 和 `libixcap.a` 或 `libixcap.so`；默认在 `/usr/local/include`、`/usr/local/lib`，可在清单覆盖 `tianlai_ixcap_include`、`tianlai_ixcap_lib`。
- 真实地址、链路、网卡 PCI 地址、光模块、驱动、NUMA、MTU 及存储挂载由现场先完成验证。`tianlai_storage_directory` 必须指向目标机已有且正确挂载的存储路径下的目录；脚本只创建目录，不验证其所在磁盘或容量。
- 清单可使用 YAML 或 INI：复制 `inventory_example.yml` 为 `inventory.yml`，或复制 `inventory_example.ini` 为 `inventory.ini`。两份示例都包含缓存服务器 1～14（节点 0～13）及同机部署的存储/控制器；选择一种维护即可。须现场核对 SSH 地址，并补齐各节点的接收 PCI 地址、万兆发送接口和地址。`controller` 与 `storage` 组必须使用同一个存储服务器主机名。
- 在清单中填写两个发送接口/源IP及存储100G口上的两个无标签子网IP。两个子网共享同一个物理100G接口，不配置VLAN；数据接口不设置默认网关。管理网仍使用`tianlai_controller_ip`。
- 消色散后端须在 `tianlai_backend_ip:tianlai_notify_port` 监听 UDP。存储服务从 `tianlai_controller_ip` 发送 `CAPTURE_RESULT`；后端持久化结果后向该报文的来源地址回复 `ACK_RESULT`。Playbook 不部署后端，也不修改相关防火墙。
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

`deploy.sh`先检查清单和语法，再执行完整部署；可附加Ansible参数，例如`bash deploy.sh --limit tianlaistore1`。默认读取`inventory.yml`，使用INI清单时执行`TIANLAI_INVENTORY=inventory.ini bash deploy.sh`。`ansible.cfg`启用15路并发和SSH pipelining，减少批量部署连接次数。

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

示例清单中的控制器地址是占位符；运行全部主机前须填写实际地址，也可以用 `--limit` 只选择已准备好的主机。盘点输出中各项含义：

| 输出项 | 用途 |
| --- | --- |
| `expected_cache_receive_ports` | 缓存服务器 1～3 各需 8 个接收端口，4～14 各需 4 个；其他角色不适用。 |
| `expected_cache_send_interfaces` | 每个缓存节点使用物理5/6口，共2个发送接口。 |
| `configured_receive_devices`、`configured_send_interfaces`、`configured_send_ips`、`configured_control_ip` | 显示清单中已有值，便于与现场盘点结果核对。 |
| `interfaces` | 系统网络接口的名称、MAC、IP、MTU、状态、链路速率、PCI 地址、驱动和 NUMA 节点。 |
| `pci_network_devices` | PCI 网络设备的地址、厂商和设备编号、驱动、NUMA 节点及其系统接口；没有系统接口的设备也会列出。 |
| `warnings` | 无法读取 IP 或系统设备信息时的提示。 |

现场确认物理接线后，将接收PCI地址填入`tianlai_receive_devices`，将物理5/6口的Linux接口名依次填入`tianlai_send_interfaces`，两个子网地址依次填入`tianlai_send_ips`。存储服务器同一个100G接口必须已配置`tianlai_storage_ip0/1`两个地址。Playbook只校验和生成报告，不自动修改主机网络。

## 执行

从本目录执行（以下以 YAML 清单为例；INI 清单把文件名改为 `inventory.ini`。切勿提交包含现场地址、凭据或私钥的清单）：

```bash
# Recommended: validates configuration and then deploys.
bash deploy.sh

# Equivalent manual commands.
ansible-playbook -i inventory.yml --syntax-check site.yml
ansible-playbook -i inventory.yml --ask-become-pass site.yml
```

完整清单通过校验后，部署会自动在每台缓存服务器和兼任控制器的存储服务器生成 `{{ tianlai_root }}/config/network-report.yaml`（默认 `/opt/tianlai-store/config/network-report.yaml`），并在存储服务器生成 `/opt/tianlai-store/config/storage.yaml`。重复部署时更新。可在对应服务器上查看：

```bash
sudo cat /opt/tianlai-store/config/network-report.yaml
```

报告中的`matched_receive_devices`和`matched_send_interfaces`将配置与现场设备对应；任一发送接口为`null`表示未找到。`speed_mbps=10000`表示当前协商为10 Gbit/s，未连接时可能为`null`。

报告仅记录配置和观察结果，不会自动选择物理端口、设置系统 IP 或修改驱动。部署前仍需按上一节完成端口对应关系和清单填写；如果完整清单校验未通过，部署及自动报告生成均不会开始，可先单独运行 `inspect_nics.yml` 盘点。

SSH登录使用私钥，sudo密码仅在本次部署进程内使用，不写入磁盘。部署会安装 apt 软件包、复制源码及工具，通过项目统一的`make production PROFILE=...`入口编译缓存节点、生成YAML和控制器节点清单；缓存节点运行`tianlai-cache`，存储服务器同时运行`tianlai-storage`和`tianlai-controller`。重跑时，源码、配置或服务变化会触发对应服务重启。**缓存服务重启会丢失全部内存缓存及尚未完成的冻结事件**；应先确认没有活动或暂停事件，并安排维护窗口。重启后必须等待约100秒重新预热，若控制器仍记录旧的未完成事件，应查询全部节点并取消无法恢复的事件，使用新的`event_id`重新触发。

服务默认使用 root，节点通过 ixcap 收包；脚本没有配置防火墙或服务用户隔离，需结合现场权限和网络策略审查。软件包安装、服务重启和数据保存路径均由执行 playbook 的操作者负责确认。首次部署或配置变更会重启服务，完成后检查：

```bash
systemctl status tianlai-storage.service
systemctl status tianlai-cache.service
systemctl status tianlai-controller.service
```

分别在对应机器执行上述命令，并按[部署与联调手册](../../部署与联调手册.md)检查 14 台收包、计数同步、触发、落盘、缺包、通知和真实吞吐。服务启动不等于缓存已 READY，也不表示真实设备验收通过。

默认每节点两个lane合计限速5.6 Gbit/s。存储服务为14台节点生成28个监听端点：lane 0使用20000～20013，lane 1使用20100～20113，并以4个ZeroMQ I/O线程服务单100G物理口。修改端口基数时应同时更新节点与存储服务。
