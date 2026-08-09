"""Ollama 本地大模型检测器。

Ollama 是主流本地模型工具。本模块负责：
- 探测 Ollama 服务是否在跑（端口 11434）
- 列出已安装的模型（GET /api/tags）
- 综合检测（是否运行、装了哪些模型、推荐的 chat/embed 模型是否就绪）

检测到 Ollama 后，LLM 抽象层指向其 OpenAI 兼容端口即可（已有能力）。
"""
from __future__ import annotations

import socket

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
OLLAMA_BASE = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"
OLLAMA_V1 = f"{OLLAMA_BASE}/v1"

# 推荐的本地模型（未装时引导用户拉取）
DEFAULT_CHAT_MODEL = "qwen2.5:7b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """检查端口是否监听。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ollama_running() -> bool:
    """探测 Ollama 服务（11434 端口）是否在跑。"""
    return port_open(OLLAMA_PORT)


def list_ollama_models() -> list[str]:
    """列出已安装的模型名。未运行或请求失败返回 []。"""
    try:
        import httpx

        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        r.raise_for_status()
        data = r.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:  # noqa: BLE001
        return []


def ensure_ollama() -> dict:
    """综合检测 Ollama 状态。

    返回 {running, models, chat_model_ready, embed_model_ready, chat_model, embed_model}。
    """
    running = ollama_running()
    models = list_ollama_models() if running else []
    return {
        "running": running,
        "models": models,
        "chat_model": DEFAULT_CHAT_MODEL,
        "embed_model": DEFAULT_EMBED_MODEL,
        "chat_model_ready": DEFAULT_CHAT_MODEL in models,
        "embed_model_ready": DEFAULT_EMBED_MODEL in models,
    }
