# 天璇 Merak-Linux

Merak 的 Linux 服务器版，跑在 2 核 4G 的便宜 VPS 上，**7×24 常驻**，解决"个人电脑无法一直开机而掉线"的问题。

> 与 Windows 版分离。本版**仅支持云端 API 模型**（DeepSeek 等），不支持本地大模型——避免 2 核 4G 服务器资源耗尽崩溃。

## 架构

```
Merak-Linux/
├── youchat/          # 核心代码（群聊 + AI朋友 + 记忆 + 人设）
├── server.py         # 启动器：检测/启动 NapCat(Docker) + 引擎 + Web UI
├── setup.sh          # 一键部署脚本（Ubuntu/CentOS 兼容）
├── deploy/
│   ├── merak.service   # systemd 服务（开机自启 + 崩溃重启）
│   └── merak-env.sh    # 环境变量（API key）
├── requirements.txt
└── README.md
```

## 前置条件

- **服务器**：2 核 4G 以上（最低 1 核 2G），Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / Rocky 9+
- **开放端口**：5173（Web UI）、6099（NapCat 面板）、6700（反向 WS，可只开本地）
- **一个 QQ 小号**（机器人账号）
- **一个 AI 模型 API key**（DeepSeek 等）

## 一键部署

```bash
# 1. 上传项目到服务器（或用 git clone）
scp -r Merak-Linux root@你的服务器IP:/opt/

# 2. 一键部署
cd /opt/Merak-Linux
sudo bash setup.sh
```

脚本自动完成：
- 安装 Python3 + Docker
- 拉取并启动 NapCat Docker 容器（端口 6099/6700）
- 安装 Python 依赖
- 配置 systemd 服务（`merak.service`）

## 配置

### 1. 填 API key

```bash
nano /opt/merak/deploy/merak-env.sh
# 把 YOUNCHAT_API_KEY=你的API_Key 改成你的真实 key
```

### 2. 启动服务

```bash
systemctl start merak
systemctl status merak      # 查看状态
journalctl -u merak -f      # 查看日志
```

### 3. NapCat 扫码登录

NapCat 跑在 Docker 里，首次启动需要扫码登录 QQ：

```bash
docker logs -f napcat
# 日志里会出现二维码，用手机 QQ 扫码登录小号
```

### 4. 配置反向 WS（一次）

1. 浏览器打开 `http://服务器IP:6099/webui`（token 在 `docker logs napcat` 的『WebUi Token:』后）
2. 网络配置 → Websocket 服务器 → 新增：Host `127.0.0.1`，Port `6700`

### 5. 用起来

1. 浏览器打开 `http://服务器IP:5173` → Web UI
2. 启动 tab → QQ 接入：填 bot_qq（小号 QQ 号）+ ws_url（`ws://127.0.0.1:6700`）→ 启动
3. 群里 @机器人 开聊

## systemd 管理

```bash
systemctl start merak       # 启动
systemctl stop merak        # 停止
systemctl restart merak     # 重启
systemctl status merak      # 状态
journalctl -u merak -f      # 实时日志
systemctl enable merak      # 开机自启（setup.sh 已启用）
```

`merak.service` 配置了 `Restart=always` + `MemoryMax=3500M`，崩溃自动重启、防资源耗尽。

## 常见问题

> 🚨 **部署遇到问题？先看 [《部署排障指南》](docs/DEPLOY_TROUBLESHOOTING.md)** ——记录了所有真实踩坑与解法（含最关键的"NapCat 必须设 Token + Host 0.0.0.0"）。

| 问题 | 处理 |
|---|---|
| 机器人收不到消息 | **NapCat 反向 WS 必须设 Token + Host 0.0.0.0 + Port 6700**（详见排障指南） |
| 服务一直重启 | `journalctl -u merak -f` 看报错；确认 merak-env.sh 的 key 填了（**别复制打码后的省略号**） |
| NapCat 面板进不去 | 确认容器在跑：`docker ps`；token 在 `docker logs napcat` |
| 服务器内存爆 | 本版已禁用本地模型/embedding，若仍高看是否多开进程 |
| 换 API 模型 | 改 merak-env.sh 的 key，config.yaml 的 base_url/chat_model |
