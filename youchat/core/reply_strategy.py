"""回复策略分析：每条消息先想"怎么回"，再生成。

像真人先想后说：分析当前这条消息该用什么态度、多长的回复、
什么情绪、是否要提往事，把策略注入生成，避免一视同仁的"客服式"回答。
"""
from __future__ import annotations

from .. import llm as llm_mod

STRATEGY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "plan_reply",
            "description": "分析当前这条群聊消息，决定机器人角色该怎么回（态度/长度/情绪/是否提往事）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "attitude": {
                        "type": "string",
                        "description": "本轮回应的态度，如：吐槽/认真/敷衍/关心/警惕/调侃/平静",
                    },
                    "length": {
                        "type": "string",
                        "enum": ["ultra_short", "short", "medium"],
                        "description": "回复长度：ultra_short=一句甚至半句；short=两到三句；medium=适当展开",
                    },
                    "emotion": {
                        "type": "string",
                        "description": "本轮回应的情绪底色，如：傲娇/不耐烦/温柔/戏谑/冷漠/热情",
                    },
                    "mention_memory": {
                        "type": "boolean",
                        "description": "是否在回复中提及过去的某段记忆（如果当前消息跟往事有关）",
                    },
                    "reason": {"type": "string", "description": "为什么这样决定（一句话）"},
                },
                "required": ["attitude", "length", "emotion", "mention_memory", "reason"],
            },
        },
    }
]

_SYSTEM_PROMPT = (
    "你是群聊回复策略师。你会看到一条别人发来的消息，以及这个群最近的一些对话。"
    "请判断该角色的机器人应该怎么回：用什么态度、说多长、带什么情绪、是否提往事。"
    "记住：真人聊天不追求每句都完整回答，可能敷衍、可能岔开、可能只说半句。"
)


def analyze_reply_strategy(llm: "llm_mod.LLMClient", character_name: str,
                           message_text: str, recent: list[str]) -> dict:
    """分析回复策略，返回 {attitude, length, emotion, mention_memory, reason}。

    失败时返回保守默认（不阻塞生成）。
    """
    recent_block = "\n".join(f"- {t}" for t in recent[-8:]) if recent else "（无近期对话）"
    user_content = (
        f"你是{character_name}。最近群里的对话：\n{recent_block}\n\n"
        f"现在{character_name}要回复这条消息：\n「{message_text}」\n\n"
        "请决定该怎么回。"
    )
    msgs = [
        llm_mod.LLMMessage(role="system", content=_SYSTEM_PROMPT),
        llm_mod.LLMMessage(role="user", content=user_content),
    ]
    try:
        result = llm.chat_structured(msgs, STRATEGY_TOOL, temperature=0.3)
        return {
            "attitude": result.get("attitude", "平静"),
            "length": result.get("length", "short"),
            "emotion": result.get("emotion", "平静"),
            "mention_memory": bool(result.get("mention_memory", False)),
            "reason": result.get("reason", ""),
        }
    except llm_mod.LLMError:
        return {"attitude": "平静", "length": "short", "emotion": "平静",
                "mention_memory": False, "reason": ""}


def strategy_to_prompt(strategy: dict) -> str:
    """把策略 dict 转成注入生成的一行引导。"""
    parts = [
        f"本轮回复的态度：{strategy.get('attitude', '平静')}",
        f"情绪底色：{strategy.get('emotion', '平静')}",
        f"长度：{'一句半句' if strategy['length']=='ultra_short' else '两三句' if strategy['length']=='short' else '适当展开'}",
    ]
    if strategy.get("mention_memory"):
        parts.append("适合自然地提一句往事")
    return "；".join(parts)
