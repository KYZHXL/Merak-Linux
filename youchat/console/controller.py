"""AppController：三形态共享的核心控制器。

职责：配置读写/校验、角色 CRUD/校验、引擎启动/停止、settings。
Web / Desktop / TUI 都是薄层，只调这里的同步接口。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import yaml

from ..runtime import Runtime, build_runtime, resolve_project_root, resolve_repo_root
from ..core.persona import load_character, REQUIRED_FIELDS

# 配置文件在 youchat 包目录下
CONFIG_FILENAME = "config.yaml"
SETTINGS_FILENAME = "settings.json"

VALID_MODES = ("web", "desktop", "tui")

_DEFAULT_SETTINGS = {
    "ui": {"mode": "web"},
    "default_role": "laomao",
    "config_path": "config.yaml",
    "db_path": "data/youchat.db",
}


class AppController:
    def __init__(self, project_root: Optional[Path] = None):
        self.pkg_root = project_root or resolve_project_root()
        self.repo_root = resolve_repo_root()
        self.runtime: Optional[Runtime] = None
        self.qq_adapter = None
        self.friend_adapter = None
        self._mock_factory = None  # 测试注入用

    # ---- 路径 ----

    @property
    def config_path(self) -> Path:
        return self.pkg_root / CONFIG_FILENAME

    @property
    def settings_path(self) -> Path:
        return self.pkg_root / SETTINGS_FILENAME

    @property
    def characters_dir(self) -> Path:
        cfg = self._load_config_raw()
        d = Path(cfg.get("storage", {}).get("characters_dir", "characters"))
        return d if d.is_absolute() else self.pkg_root / d

    def _load_config_raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    # ---- 配置 ----

    def get_config(self) -> dict:
        return self._load_config_raw()

    def validate_config(self, data: dict) -> list[str]:
        errors = []
        model = data.get("model", {})
        if not model.get("base_url"):
            errors.append("model.base_url 不能为空")
        if not model.get("chat_model"):
            errors.append("model.chat_model 不能为空")
        storage = data.get("storage", {})
        if not storage.get("db_path"):
            errors.append("storage.db_path 不能为空")
        if not storage.get("characters_dir"):
            errors.append("storage.characters_dir 不能为空")
        for section, key in [("engine", "episodic_buffer_size"), ("engine", "compact_after"),
                             ("engine", "max_recalled_memories")]:
            v = data.get("engine", {}).get(key)
            if v is not None and not isinstance(v, int):
                errors.append(f"{section}.{key} 必须是整数")
        for section, key in [("context_budget", "core"), ("context_budget", "long_term"),
                             ("context_budget", "short_term")]:
            v = data.get("context_budget", {}).get(key)
            if v is not None and not isinstance(v, int):
                errors.append(f"{section}.{key} 必须是整数")
        th = data.get("retrieval", {}).get("vector_threshold")
        if th is not None and not isinstance(th, (int, float)):
            errors.append("retrieval.vector_threshold 必须是数字")
        checker = data.get("consistency", {}).get("checker")
        if checker not in (None, "auto", "off"):
            errors.append("consistency.checker 只能是 auto 或 off")
        return errors

    def save_config(self, data: dict) -> dict:
        errors = self.validate_config(data)
        if errors:
            return {"ok": False, "errors": errors}
        yaml.safe_dump(data, self.config_path.open("w", encoding="utf-8"),
                       allow_unicode=True, sort_keys=False)
        return {"ok": True, "errors": []}

    # ---- 角色 ----

    def list_characters(self) -> list[dict]:
        out = []
        for p in sorted(self.characters_dir.glob("*.yaml")):
            try:
                ch = load_character(p)
                out.append({"character_id": ch.character_id, "name": ch.name, "file": p.name})
            except Exception as e:  # noqa: BLE001
                out.append({"character_id": p.stem, "name": "?", "file": p.name, "error": str(e)})
        return out

    def get_character(self, cid: str) -> Optional[dict]:
        path = self._char_file(cid)
        if not path.exists():
            return None
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def validate_character(self, data: dict) -> list[str]:
        errors = []
        cid = data.get("character_id", "")
        if not cid:
            errors.append("character_id 不能为空")
        elif not re.match(r"^[a-zA-Z0-9_-]+$", cid):
            errors.append("character_id 只能含字母数字_-")
        for field in REQUIRED_FIELDS:
            if not data.get(field):
                errors.append(f"{field} 不能为空")
        for i, t in enumerate(data.get("taboos", [])):
            if not t.get("text"):
                errors.append(f"taboos[{i}] 缺少 text")
        return errors

    def save_character(self, cid: str, data: dict) -> dict:
        data["character_id"] = cid
        errors = self.validate_character(data)
        if errors:
            return {"ok": False, "errors": errors}
        self._char_file(cid).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return {"ok": True, "errors": []}

    def delete_character(self, cid: str) -> dict:
        files = sorted(self.characters_dir.glob("*.yaml"))
        if len(files) <= 1:
            return {"ok": False, "error": "不能删除最后一个角色"}
        path = self._char_file(cid)
        if not path.exists():
            return {"ok": False, "error": f"角色 {cid} 不存在"}
        settings = self.get_settings()
        if settings.get("default_role") == cid:
            return {"ok": False, "error": "该角色是默认角色，请先在设置中更换"}
        path.unlink()
        return {"ok": True}

    # ---- 从文本生成角色（AI 提炼）----

    def _build_llm(self):
        """从 config.yaml 构建 LLMClient（供提炼等工具用）。"""
        from ..llm import LLMClient
        from ..runtime import resolve_path as _rp
        import os

        settings = self.get_settings()
        cfg_path = Path(settings.get("config_path", "config.yaml"))
        if not cfg_path.is_absolute():
            cfg_path = self.pkg_root / cfg_path
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        model = cfg["model"]
        api_key = os.environ.get("YOUNCHAT_API_KEY", model.get("api_key", ""))
        return LLMClient(base_url=model["base_url"], api_key=api_key, model=model["chat_model"])

    def preview_character_from_text(self, texts: list[str], base_id: str = "") -> dict:
        """AI 从用户文本提炼角色档案，返回预览 dict（不保存）。"""
        from ..core.extractor import extract_character_profile

        if not texts or not any(t.strip() for t in texts):
            return {"ok": False, "error": "请先粘贴或选择角色资料文本"}
        try:
            llm = self._build_llm()
            profile = extract_character_profile(llm, texts)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"AI 提炼失败: {e}"}
        profile["character_id"] = base_id or profile.get("character_id", "")
        return {"ok": True, "profile": profile}

    # ---- 本地大模型（Ollama）----

    def ollama_status(self) -> dict:
        """检测 Ollama 运行状态 + 已装模型。"""
        from ..core.ollama import ensure_ollama

        info = ensure_ollama()
        return {"ok": True, **info}

    def apply_ollama(self, chat_model: str, embed_model: str) -> dict:
        """把配置指向本地 Ollama 模型。"""
        from ..core.ollama import OLLAMA_V1

        cfg = self.get_config()
        model = cfg.setdefault("model", {})
        model["base_url"] = OLLAMA_V1
        model["api_key"] = ""              # 本地模型无需 key
        model["chat_model"] = chat_model
        model["embedding_model"] = f"local:ollama:{embed_model}" if embed_model else ""
        # 记录到 ollama 段
        cfg.setdefault("ollama", {})["chat_model"] = chat_model
        cfg.setdefault("ollama", {})["embed_model"] = embed_model
        res = self.save_config(cfg)
        return res

    def _char_file(self, cid: str) -> Path:
        return self.characters_dir / f"{cid}.yaml"

    # ---- 记忆沉淀库 ----

    @property
    def memory_dir(self) -> Path:
        """每角色记忆沉淀库目录（与 engine 导出同路径）。"""
        return resolve_repo_root() / "memory"

    def list_memory_files(self) -> list[dict]:
        """列出 memory/ 下所有角色的记忆库文件信息。"""
        out = []
        for ch in self.list_characters():
            cid = ch["character_id"]
            path = self.memory_dir / f"{cid}.txt"
            info = {
                "character_id": cid,
                "name": ch["name"],
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
                "mtime": path.stat().st_mtime if path.exists() else 0,
            }
            out.append(info)
        return out

    def get_memory(self, cid: str) -> dict:
        """读取指定角色的记忆沉淀库 txt 全文。文件不存在返回 {ok: false}。"""
        path = self.memory_dir / f"{cid}.txt"
        if not path.exists():
            return {"ok": False, "error": f"{cid} 还没有记忆沉淀库（跑对话触发沉淀后生成）"}
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"读取失败: {e}"}
        return {"ok": True, "content": content, "character_id": cid}

    # ---- 启动 ----

    def start(self, role: str, mock: bool = False) -> dict:
        if self.runtime is not None:
            return {"ok": False, "error": "引擎已在运行，先停止"}
        settings = self.get_settings()
        cfg_path = Path(settings.get("config_path", "config.yaml"))
        if not cfg_path.is_absolute():
            cfg_path = self.pkg_root / cfg_path
        db_path = Path(settings.get("db_path", "data/youchat.db"))
        if not db_path.is_absolute():
            db_path = self.pkg_root / db_path
        factory = (self._mock_factory or _mock_llm) if mock else None
        try:
            self.runtime = build_runtime(self.pkg_root, cfg_path, db_path, role,
                                         llm_factory=factory)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        roles = list(self.runtime.characters)
        return {"ok": True, "roles": roles, "db": str(db_path)}

    def stop(self) -> None:
        if self.runtime is not None:
            self.runtime.storage.close()
            self.runtime = None

    def status(self) -> dict:
        roles = [c["character_id"] for c in self.list_characters()]
        return {
            "running": self.runtime is not None,
            "roles": roles,
        }

    # ---- QQ 接入 ----

    def start_qq(self, role: str, bot_qq: str, ws_url: str,
                 group_allowlist: Optional[list] = None, mock: bool = False) -> dict:
        """构建 runtime + QQ 适配器并后台启动。"""
        if self.qq_adapter is not None and getattr(self.qq_adapter, "is_running", False):
            return {"ok": False, "error": "QQ 接入已在运行，先停止"}
        if not bot_qq:
            return {"ok": False, "error": "bot_qq 不能为空"}
        if not ws_url:
            return {"ok": False, "error": "ws_url 不能为空"}

        settings = self.get_settings()
        cfg_path = Path(settings.get("config_path", "config.yaml"))
        if not cfg_path.is_absolute():
            cfg_path = self.pkg_root / cfg_path
        db_path = Path(settings.get("db_path", "data/youchat.db"))
        if not db_path.is_absolute():
            db_path = self.pkg_root / db_path
        factory = (self._mock_factory or _mock_llm) if mock else None
        try:
            runtime = build_runtime(self.pkg_root, cfg_path, db_path, role, llm_factory=factory)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        from ..adapters.qq_napcat import QQNapcatAdapter

        adapter = QQNapcatAdapter(runtime.engine, bot_qq, ws_url, role=role,
                                  group_allowlist=group_allowlist)
        self.qq_runtime = runtime
        self.qq_adapter = adapter
        adapter.start()
        # 保存 settings 便于下次填充
        self.save_settings({"qq": {"bot_qq": bot_qq, "ws_url": ws_url,
                                   "group_allowlist": group_allowlist or []}})
        return {"ok": True, "role": role, "bot_qq": bot_qq, "ws_url": ws_url}

    def stop_qq(self) -> None:
        if self.qq_adapter is not None:
            self.qq_adapter.stop()
            self.qq_adapter = None
        if getattr(self, "qq_runtime", None) is not None:
            self.qq_runtime.storage.close()
            self.qq_runtime = None

    def qq_status(self) -> dict:
        running = self.qq_adapter is not None and getattr(self.qq_adapter, "is_running", False)
        return {
            "running": running,
            "role": getattr(self.qq_adapter, "role", ""),
            "bot_qq": getattr(self.qq_adapter, "bot_qq", ""),
            "ws_url": getattr(self.qq_adapter, "ws_url", ""),
        }

    # ---- AI 朋友（私聊角色扮演）----

    def start_friend(self, role: str, bot_qq: str, ws_url: str,
                     mock: bool = False) -> dict:
        """构建 runtime + 朋友适配器（私聊）并后台启动。"""
        if self.friend_adapter is not None and getattr(self.friend_adapter, "is_running", False):
            return {"ok": False, "error": "AI 朋友已在运行，先停止"}
        if not bot_qq:
            return {"ok": False, "error": "bot_qq 不能为空"}
        if not ws_url:
            return {"ok": False, "error": "ws_url 不能为空"}

        settings = self.get_settings()
        cfg_path = Path(settings.get("config_path", "config.yaml"))
        if not cfg_path.is_absolute():
            cfg_path = self.pkg_root / cfg_path
        db_path = Path(settings.get("db_path", "data/youchat.db"))
        if not db_path.is_absolute():
            db_path = self.pkg_root / db_path
        factory = (self._mock_factory or _mock_llm) if mock else None
        try:
            runtime = build_runtime(self.pkg_root, cfg_path, db_path, role, llm_factory=factory)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        from ..adapters.qq_friend import QQFriendAdapter

        adapter = QQFriendAdapter(runtime.engine, bot_qq, ws_url, role=role)
        self.friend_runtime = runtime
        self.friend_adapter = adapter
        adapter.start()
        # 保存 settings（friend_role）
        self.save_settings({"qq": {"friend_role": role, "bot_qq": bot_qq, "ws_url": ws_url}})
        return {"ok": True, "role": role, "bot_qq": bot_qq, "ws_url": ws_url}

    def stop_friend(self) -> None:
        if self.friend_adapter is not None:
            self.friend_adapter.stop()
            self.friend_adapter = None
        if getattr(self, "friend_runtime", None) is not None:
            self.friend_runtime.storage.close()
            self.friend_runtime = None

    def friend_status(self) -> dict:
        running = self.friend_adapter is not None and getattr(self.friend_adapter, "is_running", False)
        return {
            "running": running,
            "role": getattr(self.friend_adapter, "role", ""),
            "bot_qq": getattr(self.friend_adapter, "bot_qq", ""),
            "ws_url": getattr(self.friend_adapter, "ws_url", ""),
        }

    # ---- settings ----

    def get_settings(self) -> dict:
        if not self.settings_path.exists():
            return dict(_DEFAULT_SETTINGS)
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULT_SETTINGS)

    def save_settings(self, data: dict) -> dict:
        settings = self.get_settings()
        mode = data.get("mode")
        if mode is not None:
            if mode not in VALID_MODES:
                return {"ok": False, "error": f"mode 必须是 {VALID_MODES}"}
            settings.setdefault("ui", {})["mode"] = mode
        if data.get("default_role"):
            settings["default_role"] = data["default_role"]
        if "qq" in data:
            qq = data["qq"]
            settings.setdefault("qq", {}).update(
                {k: qq.get(k) for k in ("bot_qq", "ws_url", "group_allowlist", "friend_role") if k in qq}
            )
        self.settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"ok": True}


def _mock_llm():
    from ..tests.verify import MockLLM
    return MockLLM()
