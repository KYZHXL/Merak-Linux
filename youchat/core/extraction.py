"""结构化记忆沉淀（Compaction）与关键词钩子提取。

对话超过窗口后触发，用 LLM function calling 从会话中抽出：
- keyword hooks：重点词钩子（实体 / 事件 / 情绪），作为后续检索主轴
- 事件记忆条目：事件摘要 + hooks + 参与者 + 情感
- 社交演化更新：对某成员好感度 ±、新黑话、关系变化
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .models import Event, Sentiment, SocialUpdate
from .. import llm as llm_mod

# ---- function calling schema（国内模型如 DeepSeek 均支持） ----

COMPACT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "record_memories",
            "description": (
                "从一段群聊对话中提取机器人角色应该记住的内容。"
                "输出事件记忆和社交演化更新。hook 必须是短关键词（实体/事件/情绪），"
                "用于未来检索，例如：'阿伟' '修电脑' '生日派对'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "description": "值得长期记住的事件，一条一个。无关紧要的寒暄不要记录。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string", "description": "一句话事件摘要"},
                                "hooks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "2-4 个重点词钩子，涵盖实体与事件",
                                },
                                "participants": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "参与者成员名（显示名）",
                                },
                                "sentiment": {
                                    "type": "string",
                                    "enum": ["positive", "negative", "neutral"],
                                    "description": "事件对机器人角色的情感倾向",
                                },
                            },
                            "required": ["summary", "hooks", "participants", "sentiment"],
                        },
                    },
                    "social_updates": {
                        "type": "array",
                        "description": (
                            "对某个群成员社交画像的更新。只有发生明显变化才输出，"
                            "例如帮了机器人、惹怒了机器人、确立了新外号。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "member": {
                                    "type": "string",
                                    "description": "成员显示名",
                                },
                                "affinity_delta": {
                                    "type": "integer",
                                    "description": "好感度变化，范围 -3 到 +3",
                                },
                                "nickname": {"type": "string", "description": "新称呼（若有）"},
                                "interaction_style": {
                                    "type": "string",
                                    "description": "与该成员的互动风格（若有变化）",
                                },
                                "note": {"type": "string", "description": "一句观察备注（若有）"},
                            },
                            "required": ["member"],
                        },
                    },
                },
                "required": ["events", "social_updates"],
            },
        },
    }
]


def compact_conversation(
    client: "llm_mod.LLMClient",
    system_prompt: str,
    messages: list[dict],
    character_name: str,
) -> tuple[list[Event], list[SocialUpdate]]:
    """对一段对话做结构化沉淀，返回事件记忆与社交更新。"""
    transcript = "\n".join(
        f"{m['sender_name']}: {m['text']}" for m in messages
    )
    user_content = (
        f"你是{character_name}。以下是群聊最近的一段对话：\n\n{transcript}\n\n"
        "请提取这段对话里值得记住的内容。"
    )
    msgs = [
        llm_mod.LLMMessage(role="system", content=system_prompt),
        llm_mod.LLMMessage(role="user", content=user_content),
    ]
    args = client.chat_structured(msgs, COMPACT_TOOLS)

    events = []
    for e in args.get("events", []):
        try:
            sentiment = Sentiment(e.get("sentiment", "neutral"))
        except ValueError:
            sentiment = Sentiment.NEUTRAL
        events.append(
            Event(
                summary=e.get("summary", ""),
                hooks=[h for h in e.get("hooks", []) if h],
                participants=[p for p in e.get("participants", []) if p],
                sentiment=sentiment,
            )
        )

    updates = []
    for u in args.get("social_updates", []):
        updates.append(
            SocialUpdate(
                member_id="",   # 由调用方用显示名反查 member_id
                member_name=u.get("member", ""),
                affinity_delta=min(max(int(u.get("affinity_delta", 0)), -3), 3),
                nickname=u.get("nickname") or None,
                interaction_style=u.get("interaction_style") or None,
                note=u.get("note") or None,
            )
        )
    return events, updates


# ---- 消息级钩子提取（轻量，供每轮注入时同步抽取） ----

HOOKS_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "extract_hooks",
            "description": "从一条群聊消息中提取用于召回记忆的重点词钩子（实体/事件/情绪关键词）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hooks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-5 个短关键词，涵盖说话人、事件、话题",
                    }
                },
                "required": ["hooks"],
            },
        },
    }
]


def extract_message_hooks(client: "llm_mod.LLMClient", text: str) -> list[str]:
    """从一条消息抽出 hooks，用于即时匹配历史记忆。失败时退化为切词。"""
    try:
        args = client.chat_structured(
            [llm_mod.LLMMessage(role="user", content=text)],
            HOOKS_TOOL,
        )
        return [h for h in args.get("hooks", []) if h]
    except llm_mod.LLMError:
        return fallback_hooks(text)


def fallback_hooks(text: str) -> list[str]:
    """LLM 不可用时的切词兜底：按常见分隔符切出较短的词。"""
    import re

    tokens = re.split("[，。！？、\\s,.!?；;：:()\\[\\]{}\"'“”]+", text)
    return [t for t in tokens if 1 < len(t) <= 8][:5]
