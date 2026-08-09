"""QQ 私聊朋友适配器：1对1 角色扮演。

- 连 NapCat 的 websocketServers（NapCat 监听 host:port，默认 6700）
- 只处理私聊消息（message_type == "private"），群消息忽略
- 私聊都回（无需 @），固定朋友角色（friend_role）
- 独立 character_id = 独立记忆库（私聊专属亲密记忆）

复用群友适配器的连接/发送骨架；事件处理针对私聊。
"""
from __future__ import annotations

import asyncio
import json
import asyncio
import logging
import threading
from typing import Optional

from .base import Adapter

log = logging.getLogger("youchat.qqfriend")


def strip_text(message_segments: list) -> str:
    """把 message 段数组拼成纯文本（私聊没有 @ 段要剥，但图片等非文本段跳过）。"""
    parts = []
    for seg in message_segments or []:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts).strip()


class QQFriendAdapter(Adapter):
    """私聊朋友适配器。start() 起后台线程连 NapCat，stop() 停。"""

    def __init__(self, engine, bot_qq: str, ws_url: str,
                 role: str = "xiaoyan", group_allowlist: Optional[list[int]] = None):
        super().__init__(engine)
        self.bot_qq = str(bot_qq)
        self.ws_url = ws_url
        self.role = role
        self.group_allowlist = group_allowlist or None  # 保留字段（MVP 私聊全收）
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._current_ws = None
        self._running = threading.Event()
        self._req_futures: dict[str, asyncio.Future] = {}
        self._req_lock = threading.Lock()

    # ---- 启动/停止 ----

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run_loop, name="qq-friend", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_running(self) -> bool:
        return self._running.is_set() and self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:  # noqa: BLE001
            log.error("QQ 朋友适配器异常退出: %s", e)
        finally:
            self._running.clear()

    async def _run(self) -> None:
        """连 NapCat（NapCat websocketServers 监听 ws_url）。"""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        from .napcat_token import read_napcat_ws_token

        backoff = 1.0
        while self._running.is_set():
            try:
                # 每次重连前读取 token（NapCat 配置了 token 时必须带上，否则被拒绝）
                token = read_napcat_ws_token(ws_url=self.ws_url)
                connect_url = self.ws_url
                if token:
                    sep = "&" if "?" in connect_url else "?"
                    connect_url = f"{connect_url}{sep}access_token={token}"
                log.info("朋友适配器连接 NapCat %s ...", connect_url)
                async with connect(connect_url, ping_interval=20, ping_timeout=20) as ws:
                    log.info("朋友适配器已连接")
                    backoff = 1.0
                    self._current_ws = ws
                    await self._listen(ws)
            except (ConnectionClosed, OSError) as e:
                if not self._running.is_set():
                    break
                log.warning("连接断开: %s，%.0fs 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self._current_ws = None

    async def _listen(self, ws) -> None:
        while self._running.is_set():
            raw = await ws.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "action" in msg:
                self._resolve_request(msg)
            else:
                await self._handle_event(msg)

    # ---- 事件处理（只处理私聊）----

    async def _handle_event(self, ev: dict) -> None:
        if ev.get("post_type") != "message" or ev.get("message_type") != "private":
            return  # 忽略群消息等

        segments = ev.get("message", [])
        text = strip_text(segments)
        sender = ev.get("sender", {})
        user_id = str(sender.get("user_id", ""))
        nickname = sender.get("card") or sender.get("nickname") or f"QQ{user_id}"

        # 私聊都回（无需 @）
        if not text:
            return
        await asyncio.to_thread(self._handle_and_reply, user_id, nickname, text)

    def _handle_and_reply(self, user_id, nickname, text) -> None:
        from ..core.models import ChatMessage

        msg = ChatMessage(
            character_id=self.role,
            sender_id=f"qq:{user_id}",
            sender_name=nickname,
            text=text,
            scene="private",
        )
        try:
            result = self.engine.handle_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("生成失败: %s", e)
            return
        if result.reply:
            self.send_private_msg(user_id, result.reply)

    # ---- 发送 ----

    def send_private_msg(self, user_id, text: str) -> Optional[dict]:
        """发私聊消息。返回 API 响应；失败返回 None。"""
        if not self._loop:
            return None
        fut = asyncio.run_coroutine_threadsafe(
            self._api("send_private_msg", {"user_id": int(user_id), "message": text}),
            self._loop,
        )
        try:
            return fut.result(timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("私聊发送失败: %s", e)
            return None

    async def _api(self, action: str, params: dict) -> dict:
        req_id = str(len(self._req_futures) + 1)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._req_futures[req_id] = fut
        await self._send_raw({"action": action, "params": params, "echo": req_id})
        return await fut

    def _resolve_request(self, msg: dict) -> None:
        echo = msg.get("echo")
        if echo and echo in self._req_futures:
            fut = self._req_futures.pop(echo)
            if not fut.done():
                fut.set_result(msg)

    async def _send_raw(self, payload: dict) -> None:
        ws = self._current_ws
        if ws is None:
            for fid, fut in list(self._req_futures.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError("无连接"))
                self._req_futures.pop(fid, None)
            return
        await ws.send(json.dumps(payload, ensure_ascii=False))
