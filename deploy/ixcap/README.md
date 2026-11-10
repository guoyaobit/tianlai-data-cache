# ixcap standalone installation

该目录提供 ixcap SDK、许可证和2MB大页服务的独立安装。推荐从 Ansible master 一次部署到14台缓存服务器，不需要逐台登录，也不需要在命令中指定压缩包文件名。

## Inputs

- 已解压的SDK文件：`ixcap_lib.h`、`libixcap.a` 和 `init-huge-page-2MB.sh`。
- 一个许可证 TAR.GZ/TGZ：内容包含按12位网卡MAC命名的 `.lic` 文件，文件名不限。

当前 `libixcap.a` 会为每张卡读取 `/etc/ixcap/<12位大写MAC>.lic`，因此脚本会安装压缩包中的全部许可证并保留原文件名，不使用旧说明中的单个 `license.lic`。

## Ansible master deployment

在 Ansible master 的项目目录中创建软件包目录，把三个已解压的SDK文件和许可证 TAR包放入其中；该目录已被Git忽略：

```bash
mkdir -p deploy/ixcap/packages
cp /path/to/ixcap_lib.h deploy/ixcap/packages/
cp /path/to/libixcap.a deploy/ixcap/packages/
cp /path/to/init-huge-page-2MB.sh deploy/ixcap/packages/
cp /path/to/license-package.tar.gz deploy/ixcap/packages/
```

然后执行：

```bash
cd deploy/ansible
bash deploy_ixcap.sh
```

`deploy_ixcap.sh` 自动查找三个SDK文件和唯一的许可证包，将所需文件传到 `cache_nodes` 组；SDK不压缩、不解压，安装验证后删除远端临时文件。可使用 `--limit` 先验证一台：

```bash
bash deploy_ixcap.sh --limit tianlaistore1
```

软件包已经放在其他目录时，只指定目录，不指定文件名：

```bash
TIANLAI_IXCAP_PACKAGE_DIR=/path/to/packages bash deploy_ixcap.sh
```

默认每个NUMA节点申请5120个2MB大页，即每节点10 GiB。自定义数量时只设置环境变量，不指定软件包名称：

```bash
TIANLAI_IXCAP_HUGEPAGES_PER_NODE=4096 bash deploy_ixcap.sh
```

暂不立即申请大页：

```bash
TIANLAI_IXCAP_NO_START=true bash deploy_ixcap.sh
```

底层的 `install_ixcap.sh` 由 Playbook 自动传输和执行，也可用于单机故障处理。

## Installed files

```text
/usr/local/include/ixcap_lib.h
/usr/local/lib/libixcap.a
/etc/ixcap/<MAC>.lic
/usr/local/sbin/ixcap-hugepages
/etc/default/ixcap-hugepages
/etc/systemd/system/ixcap-hugepages.service
```

检查结果：

```bash
ls -l /usr/local/include/ixcap_lib.h /usr/local/lib/libixcap.a
sudo ls -l /etc/ixcap
systemctl status ixcap-hugepages.service
grep -H . /sys/devices/system/node/node*/hugepages/hugepages-2048kB/{nr_hugepages,free_hugepages}
```

安装完成后重新运行主部署脚本，生产缓存程序会自动找到 `/usr/local` 下的SDK并继续编译。
