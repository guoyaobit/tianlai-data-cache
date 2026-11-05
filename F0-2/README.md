# 缓存服务器 0～2

每台 8 个接收口，分别接收原 3060 的 8 个 packetId；每口 36 板卡，每包数据区 2304 B。
本机 packetId = SERVERID × 8 + 接收口编号。配置为 `server_00.yaml`～`server_02.yaml`。

```bash
make -j4
./tianlai-store-f0-2 server_00.yaml receive_config.yaml --check-config
sudo ./tianlai-store-f0-2 server_00.yaml receive_config.yaml
```

运行前须填写实际PCI地址、物理5/6口两个发送接口、管理/发送IP及全局采集编号。
沿用 GPU 程序命名的收包、缓存、配置和线程模块位于 `../Common/`，与另一组共用。
内存、触发、调度、存储格式和测试说明见 [总说明](../README.md)。
