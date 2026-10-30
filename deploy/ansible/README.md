# Ansible 部署

`site.yml` 在 Debian/Ubuntu 系的 systemd 主机上部署14台缓存节点，并在同一台存储服务器上部署存储接收器和控制器；不部署消色散后端，也不配置交换机、网卡地址、MTU、磁盘阵列或 ixcap。此脚本尚需在目标环境进行端到端验证。

## 前提和清单

- 控制机安装 Ansible，能通过 SSH 连接目标主机并提权；目标主机须能安装 Debian 软件包，控制器须有 Python 3.10 以上。脚本为缓存节点和存储服务器安装 libzmq 开发包，并为 C++ 存储服务安装 yaml-cpp、nlohmann-json 和编译工具。
- 每个缓存节点事先安装对应的 `ixcap_lib.h` 和 `libixcap.a` 或 `libixcap.so`；默认在 `/usr/local/include`、`/usr/local/lib`，可在清单覆盖 `tianlai_ixcap_include`、`tianlai_ixcap_lib`。
- 真实地址、链路、网卡 PCI 地址、光模块、驱动、NUMA、MTU 及存储挂载由现场先完成验证。`tianlai_storage_directory` 必须指向目标机已有且正确挂载的存储路径下的目录；脚本只创建目录，不验证其所在磁盘或容量。
- 清单可使用 YAML 或 INI：复制 `inventory_example.yml` 为 `inventory.yml`，或复制 `inventory_example.ini` 为 `inventory.ini`。两份示例都包含缓存服务器 1～14（节点 0～13）及同机部署的存储/控制器；选择一种维护即可。须现场核对 SSH 地址，并补齐各节点的接收 PCI 地址、万兆发送接口和地址。`controller` 与 `storage` 组必须使用同一个存储服务器主机名。
- 在清单的 `all.vars` 中替换 `ansible_user`、采集编号、控制器管理网 IP、后端 IP、存储高速数据网 IP 和存储目录，并核对每台主机的 `ansible_host` 和控制 IP，填写发送接口/地址。存储服务器只有一个 `ansible_host`，但控制器和落盘服务分别绑定 `tianlai_controller_ip` 与 `tianlai_storage_ip`。文档中的存储内网 IP 不能自动当作高速数据网 IP。所有计数起点必须与采集端一致。
- 消色散后端须在 `tianlai_backend_ip:tianlai_notify_port` 监听 UDP。存储服务从 `tianlai_controller_ip` 发送 `CAPTURE_RESULT`；后端持久化结果后向该报文的来源地址回复 `ACK_RESULT`。Playbook 不部署后端，也不修改相关防火墙。
- 如果目标上安装的 ixcap 是共享库，确保运行时动态链接器可找到该库。脚本只检查文件存在，不负责驱动、权限或网卡初始化。

## 部署前盘点网卡

`inspect_nics.yml` 可以在完整部署配置准备好之前单独运行。准备 `inventory.yml` 或 `inventory.ini`，填写 `ansible_user` 并核对要连接的服务器的 `ansible_host`；缓存节点的 `tianlai_server_id` 使用示例中的编号。以下命令以 YAML 清单为例，使用 INI 时将 `inventory.yml` 替换为 `inventory.ini`。控制机需要 Ansible 和 SSH 连接，目标服务器需要 `/usr/bin/python3`；安装 `iproute2` 后才能列出接口 IP。盘点不需要 sudo，不修改网络配置、驱动或服务。Ansible 会在目标机临时传送并执行盘点脚本。

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
| `expected_cache_send_interfaces` | 每个缓存节点的当前程序配置使用 1 个发送接口。 |
| `configured_receive_devices`、`configured_send_interface`、`configured_send_ip`、`configured_control_ip` | 显示清单中已有值，便于与现场盘点结果核对；`CHANGE_ME` 表示尚未填写。 |
| `interfaces` | 系统网络接口的名称、MAC、IP、MTU、状态、链路速率、PCI 地址、驱动和 NUMA 节点。 |
| `pci_network_devices` | PCI 网络设备的地址、厂商和设备编号、驱动、NUMA 节点及其系统接口；没有系统接口的设备也会列出。 |
| `warnings` | 无法读取 IP 或系统设备信息时的提示。 |

现场确认物理接线和接收设备的驱动后，将对应的 PCI 地址按端口顺序填写到各缓存节点的 `tianlai_receive_devices`；确认数据网发送口后，将其接口名和数据网 IPv4 地址分别填写到 `tianlai_send_interface` 和 `tianlai_send_ip`。PCI 地址与机箱物理端口的对应关系不能仅凭盘点结果推断；一个 PCI 功能也不能简单等同于一张物理网卡。链路未连接、驱动不支持或数据不可读时，速率等字段可能为 `null`，需要现场进一步核对。

## 执行

从本目录执行（以下以 YAML 清单为例；INI 清单把文件名改为 `inventory.ini`。切勿提交包含现场地址、凭据或私钥的清单）：

```bash
ansible-playbook -i inventory.yml site.yml --syntax-check
ansible-playbook -i inventory.yml site.yml --ask-become-pass
```

完整清单通过校验后，部署会自动在每台缓存服务器和兼任控制器的存储服务器生成 `{{ tianlai_root }}/config/network-report.yaml`（默认 `/opt/tianlai-store/config/network-report.yaml`），并在存储服务器生成 `/opt/tianlai-store/config/storage.yaml`。重复部署时更新。可在对应服务器上查看：

```bash
sudo cat /opt/tianlai-store/config/network-report.yaml
```

报告中的 `configured` 是该服务器清单里的接收 PCI 地址、发送接口和 IP，以及控制网与存储数据网配置；`matched_receive_devices` 和 `matched_send_interface` 将这些配置与现场发现的设备对应，`observed: null` 或发送接口为 `null` 表示没有找到对应设备。`interfaces`、`pci_network_devices` 和 `warnings` 保留完整盘点结果，供核查其他接口以及未绑定系统接口的 PCI 设备。接口的 `speed_mbps` 是当前链路速率，`10000` 表示当前协商为 10 Gbit/s；未连接或驱动不提供速率时可能为 `null`，不能据此排除万兆设备。报告文件权限为 `0640`，不会触发服务重启。

报告仅记录配置和观察结果，不会自动选择物理端口、设置系统 IP 或修改驱动。部署前仍需按上一节完成端口对应关系和清单填写；如果完整清单校验未通过，部署及自动报告生成均不会开始，可先单独运行 `inspect_nics.yml` 盘点。

若使用免密 sudo 或其他标准 Ansible 提权方式，可省略 `--ask-become-pass`。部署会安装 apt 软件包、复制源码及工具，通过项目统一的 `make production PROFILE=...` 入口编译缓存节点、生成 YAML 和控制器节点清单；缓存节点运行 `tianlai-cache`，存储服务器同时运行 `tianlai-storage` 和 `tianlai-controller`。重跑时，源码、配置或服务变化会触发对应服务重启。**缓存服务重启会丢失内存中尚未写盘的数据**；应先确认没有活动或暂停事件，并安排维护窗口。

服务默认使用 root，节点通过 ixcap 收包；脚本没有配置防火墙或服务用户隔离，需结合现场权限和网络策略审查。软件包安装、服务重启和数据保存路径均由执行 playbook 的操作者负责确认。首次部署或配置变更会重启服务，完成后检查：

```bash
systemctl status tianlai-storage.service
systemctl status tianlai-cache.service
systemctl status tianlai-controller.service
```

分别在对应机器执行上述命令，并按[部署与联调手册](../../部署与联调手册.md)检查 14 台收包、计数同步、触发、落盘、缺包、通知和真实吞吐。服务启动不等于缓存已 READY，也不表示真实设备验收通过。

生成的默认节点配置为双 50 秒缓冲、最长 5 秒事件、32 GiB 过渡区、48 GiB 系统预留、默认每口 CPU 不绑定和每节点约 2.286 Gbit/s 的 ZeroMQ 应用层发送速率。存储服务从 `tianlai_storage_port=20000` 开始为 14 个节点生成专属端口和网络线程；节点模板自动使用 `起始端口 + server_id`，因此需在存储数据网放通 TCP 20000～20013。修改起始端口时应同时更新全部节点与存储服务。控制器周期参数通过 `tianlai_period_us` 和 `tianlai_integration_per_trace` 同步设置。模板使用 `logFilePath: "-"`，日志进入 systemd journal。
