"""QQ OneBot11(NapCat) 适配器：WebSocket 客户端连 NapCat。

- 作为客户端连 NapCat 的 websocketServers（NapCat 监听 host:port，默认 6700）
- 群消息事件：@ 才回复（剥掉 @ 段），未 @ 只进记忆（沉淀），不刷屏
- 群里不开放命令（纯人设聊天，调试走 UI）
- 发送：send_group_msg API，request_id → asyncio.Future 映射处理响应

线程模型：适配器在独立线程里跑 asyncio loop；engine.handle_message 同步调用
（在 loop 里用 asyncio.to_thread 跑，避免阻塞 WS 接收）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Optional

from .base import Adapter

log = logging.getLogger("youchat.qq")


def strip_at_and_text(message_segments: list) -> tuple[str, list[int]]:
    """把 message 段数组拆成 (纯文本, 被@的qq列表)。@ 段本身不进文本。"""
    text_parts = []
    at_qqs = []
    for seg in message_segments or []:
        t = seg.get("type")
        if t == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq:
                at_qqs.append(qq)
        elif t == "text":
            text_parts.append(seg.get("data", {}).get("text", ""))
    return "".join(text_parts).strip(), at_qqs


class QQNapcatAdapter(Adapter):
    """WebSocket 客户端。start() 起后台线程跑 asyncio loop，stop() 停。"""

    def __init__(self, engine, bot_qq: str, ws_url: str,
                 role: str = "laomao", group_allowlist: Optional[list[int]] = None):
        super().__init__(engine)
        self.bot_qq = str(bot_qq)
        self.ws_url = ws_url
        self.role = role
        self.group_allowlist = group_allowlist or None  # None=全部群
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
        self._thread = threading.Thread(target=self._run_loop, name="qq-napcat", daemon=True)
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
            log.error("QQ 适配器异常退出: %s", e)
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
                log.info("连接 NapCat %s ...", connect_url)
                async with connect(connect_url, ping_interval=20, ping_timeout=20) as ws:
                    log.info("NapCat 已连接")
                    backoff = 1.0
                    self._current_ws = ws
                    await self._listen(ws)
            except (ConnectionClosed, OSError) as e:
                if not self._running.is_set():
                    break
                log.warning("NapCat 连接断开: %s，%.0fs 后重连", e, backoff)
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
                # 是我们发出去的 API 请求的响应
                self._resolve_request(msg)
            else:
                await self._handle_event(msg)

    # ---- 事件处理 ----

    async def _handle_event(self, ev: dict) -> None:
        if ev.get("post_type") != "message" or ev.get("message_type") != "group":
            return
        group_id = ev.get("group_id")
        if group_id is None:
            return
        if self.group_allowlist and group_id not in self.group_allowlist:
            return

        segments = ev.get("message", [])
        text, at_qqs = strip_at_and_text(segments)
        sender = ev.get("sender", {})
        user_id = str(sender.get("user_id", ""))
        nickname = sender.get("card") or sender.get("nickname") or f"QQ{user_id}"

        # 只有被 @ 才生成回复；未 @ 只进记忆（沉淀），不回
        if str(self.bot_qq) not in at_qqs:
            self._remember_only(group_id, user_id, nickname, text)
            return

        # 被 @：剥掉 @ 段，交给引擎
        if not text:
            return
        await asyncio.to_thread(self._handle_and_reply, group_id, user_id, nickname, text)

    def _remember_only(self, group_id, user_id, nickname, text) -> None:
        """未 @：消息只进引擎记忆，但不生成/发送回复。"""
        if not text:
            return
        from ..core.models import ChatMessage

        msg = ChatMessage(
            character_id=self.role,
            sender_id=f"qq:{user_id}",
            sender_name=nickname,
            text=text,
        )
        try:
            self.engine.handle_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("记忆失败: %s", e)

    def _handle_and_reply(self, group_id, user_id, nickname, text) -> None:
        from ..core.models import ChatMessage

        msg = ChatMessage(
            character_id=self.role,
            sender_id=f"qq:{user_id}",
            sender_name=nickname,
            text=text,
        )
        try:
            result = self.engine.handle_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("生成失败: %s", e)
            return
        if result.reply:
            self.send_group_msg(group_id, result.reply)

    # ---- 发送 ----

    def send_group_msg(self, group_id, text: str) -> Optional[dict]:
        """发群消息。返回 API 响应；失败返回 None。"""
        if not self._loop:
            return None
        fut = asyncio.run_coroutine_threadsafe(
            self._api("send_group_msg", {"group_id": group_id, "message": text}),
            self._loop,
        )
        try:
            return fut.result(timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("发送失败: %s", e)
            return None

    async def _api(self, action: str, params: dict) -> dict:
        """通过 WS 发 OneBot API 请求并等待响应。"""
        req_id = str(len(self._req_futures) + 1)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._req_futures[req_id] = fut
        # 把请求排进发送队列——需要持 ws 引用，用类级最新 ws
        await self._send_raw({"action": action, "params": params, "echo": req_id})
        return await fut

    def _resolve_request(self, msg: dict) -> None:
        echo = msg.get("echo")
        if echo and echo in self._req_futures:
            fut = self._req_futures.pop(echo)
            if not fut.done():
                fut.set_result(msg)

    async def _send_raw(self, payload: dict) -> None:
        """实际发送：从当前 ws 连接发。发送失败时置空 future。"""
        ws = self._current_ws
        if ws is None:
            for fid, fut in list(self._req_futures.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError("无连接"))
                self._req_futures.pop(fid, None)
            return
        await ws.send(json.dumps(payload, ensure_ascii=False))
