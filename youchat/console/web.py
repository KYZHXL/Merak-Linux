"""Web UI：Flask 后端 + 单页前端。

所有路由都是 AppController 的薄包装（同步）。换 FastAPI 只动本文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .controller import AppController

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as e:  # noqa: BLE001
    raise SystemExit("需要 flask：pip install flask") from e


def create_app(controller: AppController) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    # PyInstaller 打包后 __file__ 指向 _MEIPASS 内部，需从 sys._MEIPASS 定位静态资源
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    static_dir = base / "static"

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    # ---- 配置 ----
    @app.get("/api/config")
    def api_get_config():
        return jsonify(controller.get_config())

    @app.put("/api/config")
    def api_put_config():
        return jsonify(controller.save_config(request.get_json(force=True)))

    # ---- 角色 ----
    @app.get("/api/characters")
    def api_list_characters():
        return jsonify(controller.list_characters())

    @app.get("/api/characters/<cid>")
    def api_get_character(cid):
        data = controller.get_character(cid)
        if data is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(data)

    @app.put("/api/characters/<cid>")
    def api_put_character(cid):
        return jsonify(controller.save_character(cid, request.get_json(force=True)))

    @app.delete("/api/characters/<cid>")
    def api_delete_character(cid):
        return jsonify(controller.delete_character(cid))

    # ---- 从文本生成角色（AI 提炼预览）----
    @app.post("/api/character/preview")
    def api_character_preview():
        body = request.get_json(force=True) or {}
        return jsonify(controller.preview_character_from_text(
            body.get("texts", []), base_id=body.get("base_id", "")))

    # ---- 启动 ----
    @app.post("/api/start")
    def api_start():
        body = request.get_json(force=True) or {}
        return jsonify(controller.start(body.get("role", ""), mock=bool(body.get("mock"))))

    @app.post("/api/runtime/stop")
    def api_stop():
        controller.stop()
        return jsonify({"ok": True})

    @app.get("/api/runtime/status")
    def api_status():
        return jsonify(controller.status())

    # ---- QQ 接入 ----
    @app.post("/api/qq/start")
    def api_qq_start():
        body = request.get_json(force=True) or {}
        return jsonify(controller.start_qq(
            body.get("role", ""), body.get("bot_qq", ""), body.get("ws_url", ""),
            group_allowlist=body.get("group_allowlist"), mock=bool(body.get("mock"))))

    @app.post("/api/qq/stop")
    def api_qq_stop():
        controller.stop_qq()
        return jsonify({"ok": True})

    @app.get("/api/qq/status")
    def api_qq_status():
        return jsonify(controller.qq_status())

    # ---- AI 朋友（私聊）----
    @app.post("/api/friend/start")
    def api_friend_start():
        body = request.get_json(force=True) or {}
        return jsonify(controller.start_friend(
            body.get("role", ""), body.get("bot_qq", ""), body.get("ws_url", ""),
            mock=bool(body.get("mock"))))

    @app.post("/api/friend/stop")
    def api_friend_stop():
        controller.stop_friend()
        return jsonify({"ok": True})

    @app.get("/api/friend/status")
    def api_friend_status():
        return jsonify(controller.friend_status())

    # ---- 记忆沉淀库 ----
    @app.get("/api/memory")
    def api_list_memory():
        return jsonify(controller.list_memory_files())

    @app.get("/api/memory/<cid>")
    def api_get_memory(cid):
        return jsonify(controller.get_memory(cid))

    # ---- 本地大模型（Ollama）----
    @app.get("/api/ollama/status")
    def api_ollama_status():
        return jsonify(controller.ollama_status())

    @app.post("/api/ollama/apply")
    def api_ollama_apply():
        body = request.get_json(force=True) or {}
        return jsonify(controller.apply_ollama(
            body.get("chat_model", ""), body.get("embed_model", "")))

    # ---- settings ----
    @app.get("/api/settings")
    def api_get_settings():
        return jsonify(controller.get_settings())

    @app.put("/api/settings")
    def api_put_settings():
        return jsonify(controller.save_settings(request.get_json(force=True)))

    return app


def main(controller: AppController, host: str = "127.0.0.1", port: int = 5173) -> None:
    import os

    port = int(os.environ.get("YOUNCHAT_WEB_PORT", port))
    app = create_app(controller)
    print(f"天璇 Merak Web UI: http://{host}:{port}")
    app.run(host=host, port=port, use_reloader=False)
