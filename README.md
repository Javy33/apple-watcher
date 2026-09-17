# Apple 库存 541 防护轮询监控

通过多出口轮换、请求去重和 541 熔断恢复，持续轮询你想要的 Apple 机型。

主节点负责调度、库存状态、Bark 到货提醒、Telegram 错误提醒和本机管理页；可选工作节点只通过 SSH 执行 Apple 查询并把 NDJSON 结果回传主节点。

相同的「地区 + SKU + 查询范围/门店」跨监控账户只查询一次，结果再分发给所有订阅者。默认查询间隔为 60 秒；配置工作节点后，两个节点每 20 轮（约 20 分钟）切换，一个节点工作时另一个节点不向 Apple 发请求。541 不会立即重试，未知结果不会误报成无货。每个监控账户最多配置 2 个机型。

管理页会原子写入配置；只有查询目标变化才热重载，Bark 与提醒方式直接生效。轮换状态保存在主节点，服务重启后继续当前相位。查询失败、空结果或 Apple 拦截会显示为“未知”，不会误报成无货。

## 上游项目与 541 升级

本项目基于 [ENCHIGO/apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher) `v0.4.2`（commit `6e9395a`）扩展，保留原项目的 Rust 核心、CLI 和桌面端，并增加多账户监控、多出口轮换、管理页及通知编排，用于持续轮询你想要的机型并降低 HTTP 541 拦截对监控的影响。

针对 Apple HTTP 541，本项目将响应标记为“被拦截 / 未知”而不是“无货”，且不会在当前 Slot 内立即重试。连续 3 个不同 Query Group 返回 541 时打开出口 IP 级熔断，先冷却 2 分钟；冷却后的单次探测仍为 541 时延长为 5 分钟，成功后清零恢复。冷却状态会持久化，重启不能跳过；相同查询跨账户去重并 fan-out，避免订阅者数量放大 Apple 请求量。

## 出口 IP 与请求频率

“频率”以下统一按相邻 Apple 请求的最短间隔表示。间隔不短于 30 秒时，建议上限为：

| 独立服务器 / 出口 IP | 最短请求间隔 |
| --- | --- |
| 1 | 120 秒 / 次 |
| 2 | 60 秒 / 次 |
| 3 | 30 秒 / 次 |

需要短于 30 秒时，必须继续增加独立出口并错峰。例如使用 6 个 IP，把它们分成 3 组，每个 30 秒窗口由一组中的 2 个不同 IP 分别在第 0 秒和第 15 秒轮询，即可达到全局 15 秒 / 次。当前代码默认 60 秒轮询；6 IP、15 秒方案需要另行扩展调度，不能只把现有间隔改成 15 秒。

## 安装或升级服务器

把以下文件放在服务器同一目录：

- `install-apple-watcher.sh`
- `notify.py`
- `admin.html`
- `upstream/`（Rust 核心源码）

然后运行：

```bash
chmod +x install-apple-watcher.sh
sudo ./install-apple-watcher.sh
```

## 打开管理页

管理页只监听服务器 `127.0.0.1:1420`，不开放公网端口。在 Windows 上运行：

```powershell
ssh -N -L 1420:127.0.0.1:1420 <SSH用户>@<主机地址>
```

保持该窗口运行，再打开 <http://127.0.0.1:1420/>。也可双击本目录的 `打开监控面板.cmd`。电脑重启后需要重新运行隧道；服务器自身会随 Ubuntu 自动启动，无需手工启动监控。

如需工作节点，通过 systemd override 设置 `APPLE_WATCHER_SUB_HOST=<SSH用户>@<工作节点地址>`。工作节点不保存 Bark、Telegram、管理页或账户配置；专用 SSH key 由 `worker_gate.py` 限制为只能运行固定 60 秒间隔的 `/opt/apple-watcher/bin/apw watch`。SSH 启用 BatchMode 和主机密钥校验，轮换阶段不会等待交互输入。未设置该变量时仅使用主节点。

```bash
systemctl status apple-watcher --no-pager
journalctl -u apple-watcher -f
```
