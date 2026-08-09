"""角色档案提炼器：从用户提供的角色资料文本，用 LLM 提炼出结构化角色档案。

支持用户提供单个或多个文本（Wiki 人设 / 游戏剧情 / 台词配音等），
合并后由 AI 统一过筛，提炼成标准角色 yaml 的字段结构。
"""
from __future__ import annotations

from .. import llm as llm_mod

# 单条资料文本的最大字符数（超出截断，防上下文超限）
MAX_TEXT_CHARS = 4000
# 最多合并的文本段数
MAX_TEXTS = 8

# function calling schema（国内模型如 DeepSeek 均支持）
CHARACTER_PROFILE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "build_character_profile",
            "description": (
                "从用户提供的角色资料文本中，提炼出结构化角色档案。"
                "资料可能来自 Wiki、游戏剧情、台词等，需综合归纳出完整人设。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character_id": {
                        "type": "string",
                        "description": "建议的英文角色 ID（小写字母/数字/下划线，如 yunli）",
                    },
                    "name": {"type": "string", "description": "角色名"},
                    "personality": {
                        "type": "string",
                        "description": "性格核心。具体化，含口头禅/习惯动作，如'外冷内热，爱吐槽，被戳穿会嘴瓢'",
                    },
                    "speech_style": {
                        "type": "string",
                        "description": "说话风格。句式/语气/用词特点，如'短句、爱用反问、不卖萌、不用颜文字'",
                    },
                    "background": {
                        "type": "string",
                        "description": "背景故事。从资料中提炼的角色经历/设定/与重要人物关系",
                    },
                    "worldview": {
                        "type": "string",
                        "description": "世界观与核心价值观。角色坚持什么、相信什么、讨厌什么",
                    },
                    "taboos": {
                        "type": "array",
                        "description": "角色绝不做的事（人设底线），2-4 条",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "禁忌表述，如'绝不承认自己熬夜'"},
                                "keywords": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "触发词/同义词，如 ['熬夜','通宵']",
                                },
                                "examples": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "具体越界场景，从资料中找证据，如'角色承认昨晚通宵打游戏'",
                                },
                            },
                            "required": ["text", "keywords", "examples"],
                        },
                    },
                },
                "required": ["character_id", "name", "personality", "speech_style",
                             "background", "worldview", "taboos"],
            },
        },
    }
]

_SYSTEM_PROMPT = (
    "你是角色档案构建师。用户会提供某个角色的资料文本（可能来自 Wiki 档案、"
    "游戏剧情、台词配音等）。请综合这些资料，提炼出一个结构化的角色档案：\n"
    "- personality / speech_style / background / worldview 要具体、有细节，能撑起一个鲜活的人\n"
    "- taboos 提炼角色的行为底线，keywords 给触发词，examples 给具体越界场景（从资料找证据）\n"
    "- 所有字段用中文（character_id 用英文）"
)


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n……（资料过长已截断）"


def extract_character_profile(llm: "llm_mod.LLMClient", texts: list[str]) -> dict:
    """合并多段文本 → AI 提炼出结构化角色档案 dict。

    返回 dict 含 character_id/name/personality/speech_style/background/worldview/taboos。
    """
    cleaned = [_truncate(t) for t in texts if t and t.strip()][:MAX_TEXTS]
    if not cleaned:
        raise llm_mod.LLMError("没有可提炼的文本")

    merged = "\n\n--- 资料分隔 ---\n\n".join(cleaned)
    msgs = [
        llm_mod.LLMMessage(role="system", content=_SYSTEM_PROMPT),
        llm_mod.LLMMessage(role="user", content=f"以下是该角色的资料：\n\n{merged}"),
    ]
    profile = llm.chat_structured(msgs, CHARACTER_PROFILE_TOOL)

    # 规范化 taboos（容忍模型漏字段）
    taboos = []
    for t in profile.get("taboos") or []:
        if not t.get("text"):
            continue
        taboos.append({
            "text": str(t["text"]).strip(),
            "keywords": [str(k) for k in t.get("keywords") or []],
            "examples": [str(e) for e in t.get("examples") or []],
        })
    profile["taboos"] = taboos

    # 确保关键字段存在
    for field in ("character_id", "name", "personality", "speech_style", "background", "worldview"):
        profile.setdefault(field, "")
    return profile
