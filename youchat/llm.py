"""LLM 抽象层：统一 OpenAI 兼容协议。

通过 config 切换 base_url + key，可在 DeepSeek / 通义 / Kimi / OpenAI / Ollama 之间切换。
生产环境用 openai 官方 SDK；无 SDK 时降级为 httpx 直接调 /chat/completions。
"""
from __future__ import annotations

import json
from dataclasses import dataclass


class LLMError(Exception):
    pass


@dataclass
class LLMMessage:
    role: str          # "system" | "user" | "assistant"
    content: str
    name: str = ""     # 群聊里模拟多说话人：user 消息带说话人名字


def _to_api(messages: list[LLMMessage]) -> list[dict]:
    out = []
    for m in messages:
        d = {"role": m.role, "content": m.content}
        if m.name:
            d["name"] = m.name
        out.append(d)
    return out


class LLMClient:
    """极简 OpenAI 兼容客户端，支持 function calling（结构化提取依赖它）。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._backend = self._init_backend()

    def _init_backend(self):
        try:
            from openai import OpenAI

            self._openai_client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            return "openai"
        except ImportError:
            try:
                import httpx  # noqa: F401

                return "httpx"
            except ImportError:
                raise LLMError("需要 openai 或 httpx 之一作为 HTTP 后端")

    # ---- 底层请求 ----

    def _request(self, messages: list[LLMMessage], temperature: float,
                 tools: list[dict] | None, max_tokens: int) -> dict:
        """返回归一化的响应 dict：{"text": str, "tool_args": dict | None}。"""
        payload: dict = {
            "model": self.model,
            "messages": _to_api(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "required" if len(tools) == 1 else "auto"

        if self._backend == "openai":
            try:
                resp = self._openai_client.chat.completions.create(**payload)
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"OpenAI 请求失败: {e}") from e
            if not resp.choices:
                raise LLMError("模型返回空 choices")
            choice = resp.choices[0]
            msg = choice.message
            if msg.tool_calls:
                args = json.loads(msg.tool_calls[0].function.arguments)
                return {"text": "", "tool_args": args}
            return {"text": msg.content or "", "tool_args": None}
        else:
            return self._request_httpx(payload, tools)

    def _request_httpx(self, payload: dict, tools: list[dict] | None) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=120) as client:
                r = client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.HTTPError as e:
            raise LLMError(f"HTTP 请求失败: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"模型 API 错误 {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not data.get("choices"):
            raise LLMError("模型返回空 choices")
        choice = data["choices"][0]
        msg = choice.get("message", {})
        if msg.get("tool_calls"):
            args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
            return {"text": "", "tool_args": args}
        return {"text": msg.get("content", "") or "", "tool_args": None}

    # ---- 对外接口 ----

    def chat(self, messages: list[LLMMessage], temperature: float = 0.9,
             tools: list[dict] | None = None, max_tokens: int = 800) -> str:
        """普通对话，返回纯文本。若模型走了 tool call 则返回空串（调用方应避免该情况）。"""
        resp = self._request(messages, temperature, tools, max_tokens)
        return resp["text"]

    def chat_structured(self, messages: list[LLMMessage], tools: list[dict],
                        temperature: float = 0.2) -> dict:
        """带 function calling 的结构化提取：强制模型调用给定 tool 并返回其参数 dict。"""
        resp = self._request(messages, temperature, tools, max_tokens=2000)
        if resp["tool_args"] is None:
            raise LLMError("结构化提取失败：模型未调用 tool")
        return resp["tool_args"]
