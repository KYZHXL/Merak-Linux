"""引擎构建逻辑抽取：供 launcher / Web / Desktop / TUI 三形态复用。

从 local_shell.main() 抽出，三前端只调 build_runtime() 得到 Runtime，
不重复造引擎构建链路。local_shell.main() 委托本模块，行为完全不变。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import llm as llm_mod
from .core.models import Character
from .core.persona import load_characters_dir
from .core.storage import Storage
from .engine import YouChatEngine


@dataclass
class Runtime:
    """一次引擎构建的结果，三前端共享。"""

    config: dict
    llm: "llm_mod.LLMClient"
    characters: dict[str, Character]
    storage: Storage
    engine: YouChatEngine


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resolve_project_root() -> Path:
    """返回 youchat 包目录（config.yaml / characters/ 所在处）。

    PyInstaller 打包后：外部可编辑目录 = exe 所在目录（cwd）。
    源码运行：youchat 包目录。
    """
    if _is_frozen():
        return Path.cwd()
    return Path(__file__).resolve().parents[0]


def resolve_repo_root() -> Path:
    """返回仓库根（含 examples/ 等资源目录）。"""
    return resolve_project_root().parent


def resolve_path(root: Path, p: str, default: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else root / path


def load_config(config_path: Path) -> dict:
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


def build_runtime(
    project_root: Path,
    config_path: Path,
    db_path: Path,
    role: str,
    llm_factory=None,
) -> Runtime:
    """构建完整运行时：config → LLM → characters → Storage → Engine。

    llm_factory 可注入（测试用 MockLLM）；缺省用真实 LLMClient。
    role 不存在时抛 ValueError（调用方决定如何展示）。
    """
    config = load_config(config_path)
    characters_dir = resolve_path(project_root, config["storage"]["characters_dir"], "characters")
    characters = load_characters_dir(characters_dir)
    if role not in characters:
        raise ValueError(f"角色 {role} 不存在，可用: {list(characters)}")

    model_cfg = config["model"]
    if llm_factory is not None:
        llm = llm_factory()
    else:
        api_key = os.environ.get("YOUNCHAT_API_KEY", model_cfg.get("api_key", ""))
        llm = llm_mod.LLMClient(
            base_url=model_cfg["base_url"],
            api_key=api_key,
            model=model_cfg["chat_model"],
        )

    storage = Storage(db_path)
    engine = YouChatEngine(storage, llm, characters, config)
    return Runtime(config=config, llm=llm, characters=characters, storage=storage, engine=engine)


def start_local_shell(runtime: Runtime, role: str, script_path: Optional[Path] = None) -> None:
    """用 Runtime 构造 LocalShell 并启动。script_path 提供时走脚本模式。"""
    from .adapters.local_shell import LocalShell

    shell = LocalShell(runtime.engine, runtime.config, role)
    if script_path:
        shell._script_mode = True
        shell._script_lines = script_path.read_text(encoding="utf-8").splitlines()
    else:
        shell._script_mode = False
    try:
        shell.start()
    finally:
        runtime.storage.close()
