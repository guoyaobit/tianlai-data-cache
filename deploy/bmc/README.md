# BMC power control

`bmc_power.sh`在管理节点通过IPMI 2.0 LAN Plus控制天籁服务器电源，不依赖服务器操作系统或SSH。地址来自现有部署文档：`tianlai1～4`为`172.0.0.200～203`，`tianlaistore1～14`为`172.0.0.204～217`，`tianlairaid`为`172.0.0.218`。这些地址部署前必须现场核对。

安装客户端：

```bash
sudo apt install ipmitool
```

查询14台缓存服务器电源状态：

```bash
cd deploy/bmc
./bmc_power.sh --user ADMIN status cache
```

启动一台或全部缓存服务器：

```bash
./bmc_power.sh --user ADMIN on tianlaistore1
./bmc_power.sh --user ADMIN on cache
```

优先使用ACPI软关机：

```bash
./bmc_power.sh --user ADMIN off tianlaistore1
```

只有在操作系统无法关机时才强制断电；该操作可能造成数据或文件系统损坏：

```bash
./bmc_power.sh --user ADMIN force-off tianlaistore1
```

所有改变电源状态的操作都会列出目标，并要求输入动作名称确认。自动化时可显式添加`--yes`。密码默认使用隐藏输入，不保存到文件；也可临时通过环境变量传入，避免出现在命令行参数或Shell历史中：

```bash
read -r -s -p 'BMC password: ' IPMI_PASSWORD; echo
export IPMI_PASSWORD
./bmc_power.sh --user ADMIN status cache
unset IPMI_PASSWORD
```

可使用`compute`、`cache`、`storage`和`all`分组，也可以一次指定多个主机名。地址文件默认为`bmc_hosts.conf`；现场地址不同应修改该文件，或通过`TIANLAI_BMC_HOSTS`指定另一个同格式文件。连接超时默认20秒，可通过`BMC_TIMEOUT_SECONDS`调整。

若连接失败，检查管理节点到BMC网段的路由、UDP 623、防火墙、HDM/IPMI LAN权限、账号权限及BMC是否启用了IPMI over LAN。GPU01～GPU24的BMC地址未记录在现有文档中，因此没有加入地址文件。
