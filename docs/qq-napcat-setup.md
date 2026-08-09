# 接入 QQ：NapCat 安装与配置指引

NapCat 是一个 QQ 无头客户端，实现了 OneBot 11 协议。你需要在电脑上运行 NapCat（用一个 QQ 号登录，建议小号），我们的机器人通过反向 WebSocket 连上它，就能在群里收发消息。

## 一、下载与安装

1. 打开 GitHub 项目 **NapNeko/NapCatQQ** 的 [Releases 页面](https://github.com/NapNeko/NapCatQQ/releases)
2. 下载 Windows 版本（`NapCat.Shell.zip` 或一键安装脚本，选最新稳定版）
3. 解压到任意目录（如 `D:\NapCat`）

## 二、启动与登录

1. 运行 NapCat（Windows 下双击启动脚本，或用 `napcat.bat`）
2. 会弹出一个 QQ 登录窗口，**用手机 QQ 扫码登录**
3. 登录成功后，NapCat 常驻后台，保持运行（不要关窗口）

> ⚠️ **务必用 QQ 小号**：机器人账号长期挂机有风险，别用主号。

## 三、开启反向 WebSocket（关键）

1. 浏览器打开 NapCat 管理面板。**最简单的方式：运行 `python start.py`，它会在终端里自动打印带 token 的完整链接，直接点开即可登录**（如 `http://127.0.0.1:6099/webui?token=xxx`）
2. 如果没跑 start.py，也可以手动访问 **http://127.0.0.1:6099/webui**，登录 token 在 NapCat 启动日志的「WebUi Token:」后面（首次启动随机生成，每个人不同）
3. 进入 **网络配置** → 找到 **Websocket 服务器**（Server）→ 点击新增
4. 填写：
   - **Host**：`127.0.0.1`
   - **Port**：`6700`
   - 其他选项默认即可（事件订阅保持全开）
5. 保存后，NapCat 会监听 6700 端口，机器人适配器连上即成功

> 端口 `6700` 是我们适配器的默认监听端口，可自定义但要在 UI 里保持一致。

## 四、验证

NapCat 面板确认反向 WS 已连接后，就可以：

1. 启动 YOUchat，进入 **Web UI**（`python -m youchat --mode web`）→ 启动 tab → **QQ 接入**卡片
2. 填两个关键值：
   - **bot_qq**：你 NapCat 登录的那个 QQ 号
   - **ws_url**：`ws://127.0.0.1:6700`（与 NapCat 里填的一致）
3. 点 **启动 QQ 接入**，状态变绿
4. 在单人测试群里 **@机器人**，它就会以你选的角色人设回复

## 五、常见问题

| 问题 | 处理 |
|---|---|
| 面板连不上 | 确认 NapCat 进程在运行；面板端口默认 6099 |
| 反向 WS 连不上 | 确认 URL 填 `ws://`（不是 http）；确认机器人和 NapCat 在同一台机器 |
| @了没反应 | 确认 bot_qq 填的是 NapCat 登录的号；确认事件订阅没被关掉 |
| 机器人乱回复 | 检查 UI 里选的角色；@ 时才会回，未@只进记忆 |
