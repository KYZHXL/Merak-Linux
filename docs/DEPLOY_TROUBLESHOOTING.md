# Merak-Linux 部署排障指南

> 本指南记录 Linux 服务器版部署过程中遇到的**全部真实问题**与解决方案。
> 每个坑都是实际踩过的，按"现象 → 原因 → 解决"写，方便对照自查。

---

## 目录

1. [准备工作：服务器规格与镜像选择](#1-准备工作服务器规格与镜像选择)
2. [获取项目（国内网络）](#2-获取项目国内网络)
3. [一键部署的坑](#3-一键部署的坑)
4. [NapCat Docker 配置的坑](#4-napcat-docker-配置的坑)
5. [WebSocket 连不上的根因（token 与 Host）](#5-websocket-连不上的根因token-与-host)
6. [中文编码崩溃（API Key 打码）](#6-中文编码崩溃api-key-打码)
7. [systemd 服务排障](#7-systemd-服务排障)
8. [常见问题速查表](#8-常见问题速查表)

---

## 1. 准备工作：服务器规格与镜像选择

### 最低配置
- **2 vCPU / 2 GiB 内存 / 40 GiB 磁盘**（阿里云轻量 ¥45/月档实测可跑）
- 内存 1.6G 实际可用，**必须加 swap 兜底**（见下文）

### 镜像选择
| 镜像 | 结论 |
|---|---|
| **Ubuntu 22.04 / 24.04** | ✅ **首选**，`setup.sh` 的 apt 路径最成熟 |
| Debian 12 | ✅ 兼容（同为 apt 系） |
| Alibaba Cloud Linux / CentOS / Rocky | ⚠️ 能用但 yum 分支，非首选 |
| OpenClaw / Hermes / Docker 应用镜像 | ❌ **不要选**，预装别的东西会和 NapCat 冲突 |

### 登录方式
- 用阿里云 **Workbench 密码登录**（默认 root，权限完整）
- 公网 IP 在控制台看（内网 `172.x` 是私有 IP，**访问要用公网 IP**）

---

## 2. 获取项目（国内网络）

### 现象
`git clone https://github.com/...` 卡住或报 `Failure when receiving data from the peer`。

### 原因
国内服务器直连 GitHub 不稳定/被墙。

### 解决：用国内加速镜像
```bash
git clone https://ghfast.top/https://github.com/KYZHXL/Merak-Linux.git /opt/Merak-Linux
```
备选镜像：`ghproxy.net` / `gh-proxy.com` / `mirror.ghproxy.com`（改前缀即可）。

---

## 3. 一键部署的坑

### 3.1 内存不足 → 先加 swap
```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### 3.2 Docker 装不上（国内）
**现象**：`setup.sh` 装 Docker 时 `curl: (35) Recv failure` + `gpg: no valid OpenPGP data`。
**原因**：脚本从 `download.docker.com` 拉密钥，国内连不上。
**解决**：用阿里云 Docker 源手动装：
```bash
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt update -y && apt install -y docker-ce docker-ce-cli containerd.io
```

### 3.3 apt 锁冲突（unattended-upgrades）
**现象**：`Could not get lock /var/lib/dpkg/lock-frontend`。
**原因**：系统自动更新服务占着锁。
**解决**：
```bash
systemctl stop unattended-upgrades && systemctl disable unattended-upgrades
```

### 3.4 缺 python3-venv
**现象**：`The virtual environment was not created successfully because ensurepip is not available`。
**解决**：
```bash
apt install -y python3.12-venv
```

---

## 4. NapCat Docker 配置的坑

### 4.1 Docker 镜像拉不动（国内）
**现象**：`docker pull` 卡住不动。
**解决**：配置 Docker 镜像加速器：
```bash
mkdir -p /etc/docker && cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://docker.mirrors.ustc.edu.cn"
  ]
}
EOF
systemctl daemon-reload && systemctl restart docker
```

### 4.2 启动容器
```bash
docker run -d --name napcat --restart=always -p 6099:6099 -p 6700:6700 -e NAPCAT_UID=0 -e NAPCAT_GID=0 mlikiowa/napcat-docker:latest
```

### 4.3 扫码登录
```bash
docker logs -f napcat
```
日志里找二维码，手机 QQ 扫码登录小号。

### 4.4 容器内没有 netstat/ss
**现象**：`docker exec napcat netstat` 报 `command not found`。
**说明**：容器精简，无网络工具。用宿主机 `netstat -tlnp` 或测 WebSocket 代替。

---

## 5. WebSocket 连不上的根因（token 与 Host）

### ⚠️ 这是最核心的坑，务必看

### 现象
- 适配器一直 `did not receive a valid HTTP response`
- NapCat 日志显示 `WebSocket服务: 127.0.0.1:6700, 已启动`
- 但宿主机连 6700 握手失败

### 根因（NapCat 的安全设计）
**未配置 Token 时，Host 被强制限制为 `127.0.0.1`**（容器内回环地址）→ 容器外宿主机连不进去！

NapCat 面板会提示："未配置Token时，Host将被强制限制为 127.0.0.1"

### 正确配置（关键！）
在 NapCat 面板（`http://公网IP:6099/webui`）的「网络配置 → Websocket 服务器」：
1. **Token 必须设置**（不能留空）——如 `merak`
2. **Host 填 `0.0.0.0`**（token 非空时允许放开）
3. Port `6700`，保存

### 验证 WebSocket 通不通
```bash
cd /opt/merak && ./venv/bin/python -c "
import asyncio
async def t():
    from websockets.asyncio.client import connect
    async with connect('ws://127.0.0.1:6700?access_token=merak', open_timeout=5) as ws:
        print('握手成功！')
asyncio.run(t())
"
```

### 适配器自动带 token
Merak 的 `napcat_token.py` 已支持从 **Docker 容器读 token**（`docker exec napcat`）。只要 NapCat 配了 token，适配器连接时自动带上 `?access_token=xxx`。

---

## 6. 中文编码崩溃（API Key 打码）

### 现象
- `生成失败: 'ascii' codec can't encode characters in position 15-41`
- API 请求直接 `UnicodeEncodeError`

### 根因（最隐蔽的坑！）
**API Key 里混入了打码的省略号字符！** 终端/网页把 key 显示成 `sk-345ba••••`，但如果你**复制的是打码后的省略号**（`••••`，UTF-8 全角省略号 `…`），写进文件的就是**带非 ASCII 字符的假 key** → Authorization header 编码崩。

验证方法：
```bash
grep API_KEY deploy/merak-env.sh | cut -d= -f2 | od -c | head -3
# 如果出现 `342 200 242`（UTF-8 省略号）就是打码了
```

### 解决
写入**完整正确的 key**（DeepSeek 官网复制的完整 `sk-` 开头 35 位）：
```bash
echo 'YOUNCHAT_API_KEY=sk-完整key' > /opt/merak/deploy/merak-env.sh
systemctl restart merak
```

### 判断 key 是否正确
```bash
cd /opt/merak && YOUNCHAT_API_KEY=$(grep API_KEY deploy/merak-env.sh | cut -d= -f2) ./venv/bin/python -c "
import os, httpx
r = httpx.post('https://api.deepseek.com/v1/chat/completions',
    json={'model':'deepseek-chat','messages':[{'role':'user','content':'你好'}]},
    headers={'Authorization':f'Bearer {os.environ.get(\"YOUNCHAT_API_KEY\")}'}, timeout=30)
print('状态:', r.status_code, '| key长度:', len(os.environ['YOUNCHAT_API_KEY']))
"
```
`状态: 200` 即正常。

---

## 7. systemd 服务排障

### 常用命令
```bash
systemctl status merak        # 状态
journalctl -u merak -f        # 实时日志
systemctl restart merak       # 重启
```

### 7.1 服务启动失败（docker 权限）
**现象**：`permission denied while trying to connect to the docker API`。
**原因**：`merak` 用户没权限访问 docker.sock。
**解决**：
```bash
usermod -aG docker merak && systemctl restart merak
```

### 7.2 服务找不到 config.yaml
**现象**：Web UI 报 `No such file or directory: 'config.yaml'`。
**原因**：`AppController(REPO)` 传了项目根，但 config.yaml 在 `youchat/` 子目录。
**解决**：用 `AppController()`（默认定位到 youchat/ 包目录）。
```bash
sed -i 's/AppController(REPO)/AppController()/g' /opt/merak/server.py
systemctl restart merak
```

### 7.3 Mock LLM 报错（Linux 版无 tests）
**现象**：勾选"用 Mock LLM"时 `ModuleNotFoundError: No module named 'youchat.tests'`。
**原因**：Linux 版部署排除了 tests/ 目录。
**解决**：**不要勾选 Mock**，用真实 API（key 配好后即可）。

### 7.4 2G 内存机器注意
- `merak.service` 默认 `MemoryMax=3500M`，2G 机器建议改成 `1800M`：
```bash
sed -i 's/MemoryMax=3500M/MemoryMax=1800M/' /opt/merak/deploy/merak.service
systemctl daemon-reload && systemctl restart merak
```

---

## 8. 常见问题速查表

| 现象 | 原因 | 解决 |
|---|---|---|
| 群 @ 没反应 | 适配器没连上 NapCat | 检查 NapCat 面板 Websocket 服务器：**必须设 Token + Host 0.0.0.0 + Port 6700** |
| 生成失败 ascii 编码 | API key 打码/带省略号 | 用完整 key 覆盖 merak-env.sh |
| Web UI 显示运行中但没回复 | Mock LLM 未装 | 取消勾选 Mock，用真实 API |
| NapCat 面板进不去 | 防火墙/容器没跑 | 阿里云防火墙放行 6099；`docker ps` 看容器 |
| 服务一直重启 | key 没配 / 报错 | `journalctl -u merak -f` 看具体报错 |
| git clone 卡住 | GitHub 被墙 | 用 ghfast.top 镜像 |
| docker pull 卡住 | Docker Hub 慢 | 配 daemon.json 加速器 |

---

## 部署成功标准

1. `systemctl status merak` → `active (running)`
2. `docker ps` → `napcat Up`
3. API 测试 → `状态: 200`
4. Web UI `http://公网IP:5173` → QQ 接入显示「运行中」
5. 群里 @机器人 → 收到符合人设的回复

如果上面 5 条都满足，Merak-Linux 部署成功！🎉
