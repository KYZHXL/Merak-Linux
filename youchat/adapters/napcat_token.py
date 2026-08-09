"""从 NapCat 配置文件读取反向 WS 的 token。

NapCat 反向 WS 服务器（websocketServers）配置了 token 时，客户端连接
必须带 token（URL ?access_token=xxx），否则握手后 NapCat 拒绝并断开。

token 存在 NapCat/config/onebot11_<qq>.json 的 network.websocketServers[] 里。
本模块负责读取匹配 ws_url 端口的那一项的 token。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from urllib.parse import urlparse


def _find_napcat_dir() -> Path:
    """定位 NapCat 目录：优先当前工作目录（打包版 exe 旁），其次仓库根。"""
    for candidate in (Path.cwd(), Path.cwd().parent):
        if (candidate / "NapCat").exists():
            return candidate / "NapCat"
    return Path.cwd() / "NapCat"


def read_napcat_ws_token(napcat_dir: Path | None = None, ws_url: str = "") -> str:
    """从 NapCat config/onebot11_*.json 读取匹配 ws_url 端口的 websocketServers token。

    返回 token 字符串；找不到返回空串。
    """
    napcat_dir = napcat_dir or _find_napcat_dir()
    port = urlparse(ws_url).port if ws_url else None

    config_dir = napcat_dir / "config"
    for f in sorted(glob.glob(str(config_dir / "onebot11_*.json"))):
        try:
            cfg = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for ws in cfg.get("network", {}).get("websocketServers", []):
            if not ws.get("enable", True):
                continue
            if port is not None and ws.get("port") != port:
                continue
            token = ws.get("token", "")
            if token:
                return token
    return ""
