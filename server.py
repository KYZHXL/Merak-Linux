"""天璇 Merak-Linux 服务器启动器（替代 Windows 的 start.py）。

Linux 版职责：
1. 检测/启动 NapCat（Docker 容器）
2. 读 settings 自动连 QQ 机器人
3. 启动 Web UI（Flask）

由 systemd 管理（merak.service），崩溃自动重启。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent
NAPCAT_CONTAINER = "napcat"
NAPCAT_IMAGE = "mlikiowa/napcat-docker:latest"
WEBUI_PORT = 6099
REVERSE_WS_PORT = 6700
MERAK_PORT = int(os.environ.get("YOUNCHAT_WEB_PORT", "5173"))


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _docker(cmd: list[str]) -> str:
    """执行 docker 命令，返回 stdout。失败抛异常。"""
    r = subprocess.run(["docker", *cmd], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(cmd)} 失败: {r.stderr.strip()}")
    return r.stdout.strip()


def napcat_container_exists() -> bool:
    try:
        return bool(_docker(["inspect", NAPCAT_CONTAINER]))
    except Exception:
        return False


def napcat_container_running() -> bool:
    try:
        state = _docker(["inspect", "-f", "{{.State.Running}}", NAPCAT_CONTAINER])
        return state == "true"
    except Exception:
        return False


def ensure_napcat() -> bool:
    """确保 NapCat Docker 容器运行。返回是否就绪。"""
    if napcat_container_running():
        print(f"  ✓ NapCat 容器已运行（{NAPCAT_CONTAINER}）")
        return True

    if napcat_container_exists():
        print("  启动 NapCat 容器...")
        _docker(["start", NAPCAT_CONTAINER])
    else:
        print(f"  创建 NapCat 容器（{NAPCAT_IMAGE}）...")
        _docker([
            "run", "-d", "--name", NAPCAT_CONTAINER, "--restart=always",
            "-p", f"{WEBUI_PORT}:{WEBUI_PORT}",
            "-p", f"{REVERSE_WS_PORT}:{REVERSE_WS_PORT}",
            "-e", "NAPCAT_UID=0", "-e", "NAPCAT_GID=0",
            NAPCAT_IMAGE,
        ])
        print("  ✓ 容器已创建（首次启动请 docker logs napcat 看二维码扫码登录）")

    # 等 WebUI 就绪
    for _ in range(60):
        if port_open(WEBUI_PORT):
            print(f"  ✓ NapCat 就绪（面板 {WEBUI_PORT}）")
            return True
        time.sleep(1)
    print("  ⚠️ NapCat 未就绪（可能还在登录），继续...")
    return False


def auto_connect_qq() -> bool:
    """读 settings 自动连 QQ 机器人。"""
    settings_path = REPO / "youchat" / "settings.json"
    if not settings_path.exists():
        print("  ✗ 未找到 settings.json（首次请在 Web UI 填 bot_qq）")
        return False
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    qq = settings.get("qq", {})
    bot_qq = str(qq.get("bot_qq", "") or "")
    ws_url = str(qq.get("ws_url", "") or f"ws://127.0.0.1:{REVERSE_WS_PORT}")
    role = settings.get("default_role", "laomao")
    if not bot_qq:
        print("  ✗ settings 里没存 bot_qq（请先在 Web UI 的 QQ 接入填一次）")
        return False

    sys.path.insert(0, str(REPO))
    from youchat.console.controller import AppController

    ctrl = AppController()  # 默认 pkg_root = youchat/ 包目录（config.yaml 所在）
    res = ctrl.start_qq(role, bot_qq, ws_url)
    if res.get("ok"):
        print(f"  ✓ QQ 机器人已启动（{ws_url}，角色 {role}）")
        return True
    print(f"  ✗ 启动失败: {res.get('error')}")
    return False


def main() -> int:
    print("=" * 48)
    print("  天璇 Merak-Linux 服务器")
    print("=" * 48)

    print("\n[1/3] 检查 NapCat...")
    ensure_napcat()

    print("\n[2/3] 检查反向 WS...")
    if port_open(REVERSE_WS_PORT):
        print(f"  ✓ 反向 WS 已就绪（端口 {REVERSE_WS_PORT}）")
    else:
        print("  ⚠️ 反向 WS 未就绪（NapCat 启动后需在 WebUI 配 Websocket 服务器，端口 6700）")

    print("\n[3/3] 启动...")
    auto_connect_qq()

    # 启动 Web UI
    print(f"\n启动 Web UI: http://0.0.0.0:{MERAK_PORT}")
    sys.path.insert(0, str(REPO))
    from youchat.console.web import create_app, main as web_main
    from youchat.console.controller import AppController

    ctrl = AppController()  # 默认 pkg_root = youchat/ 包目录（config.yaml 所在）
    web_main(ctrl, host="0.0.0.0", port=MERAK_PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
