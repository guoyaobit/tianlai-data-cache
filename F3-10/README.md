# 缓存服务器 3～10

每台 4 个接收口，对应原 4090 的 2 个 globalGpuId；每口 18 板卡，每包数据区 8832 B。
packetId = 24 + (SERVERID - 3) × 2 + 接收口编号 / 2（整数除法）。
偶数口为 X、板卡 0～17；奇数口为 Y、板卡 18～35。
配置为 `server_03.yaml`～`server_10.yaml`。

```bash
make -j4
./tianlai-store-f3-10 server_03.yaml receive_config.yaml --check-config
sudo ./tianlai-store-f3-10 server_03.yaml receive_config.yaml
```

运行前须填写实际 PCI 地址、发送接口、管理/发送 IP 及全局采集编号。
沿用 GPU 程序命名的收包、缓存、配置和线程模块位于 `../Common/`，与另一组共用。
内存、触发、调度、存储格式和测试说明见 [总说明](../README.md)。
