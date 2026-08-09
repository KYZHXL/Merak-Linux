"""角色说话风格档案（Speaking Profile）。

从角色的参考语料 + 自动积累的回话中，用 AI 提炼出"说话方式特征"，
形成一份约 500 字、随聊天不断增补的风格档案，前置注入生成提示词。

核心：
- extract_speech_style(llm, corpus_texts, existing_profile)：提炼/增量合并 ~500字
- 语料：角色 yaml 的 reference_speech + 角色自己说过的回话（storage.get_corpus）
"""
from __future__ import annotations

from .. import llm as llm_mod

# 风格档案目标长度（字符），增量合并后始终压缩到 ~500 字
TARGET_CHARS = 500
# 单次提炼最多看的语料条数
MAX_CORPUS = 60

SPEECH_STYLE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "build_speech_style",
            "description": (
                "从角色的说话语料中，提炼出一份约500字的说话方式档案。"
                "描述该角色怎么说话：句式、用词、口头禅、节奏、情绪表达。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sentence_pattern": {"type": "string", "description": "句式特点，如'短句为主，爱用反问'"},
                    "word_usage": {"type": "string", "description": "用词习惯/口头禅，如'常用啧/行行行，不用语气词'"},
                    "rhythm": {"type": "string", "description": "节奏，如'说话带停顿，偶尔说半句'"},
                    "emotion_expression": {"type": "string", "description": "情绪表达方式，如'吐槽掩盖关心，被戳穿会嘴瓢'"},
                    "full_profile": {
                        "type": "string",
                        "description": "整合后的完整说话风格档案，约500字，涵盖上述所有维度，用第二人称'你'描述",
                    },
                },
                "required": ["sentence_pattern", "word_usage", "rhythm", "emotion_expression", "full_profile"],
            },
        },
    }
]

_SYSTEM_PROMPT = (
    "你是说话风格分析师。用户会提供某角色的说话语料（台词/对话样本），"
    "请提炼该角色'怎么说'的特征，输出一份约 500 字的说话风格档案。"
    "重点：不是内容，而是表达方式——句式长短、用词习惯、口头禅、节奏停顿、情绪如何流露。"
)


def _truncate(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n……（过长截断）"


def extract_speech_style(llm: "llm_mod.LLMClient", corpus_texts: list[str],
                         existing_profile: str = "") -> dict:
    """从语料提炼说话风格档案。existing_profile 存在时增量合并（压缩到 ~500 字）。

    返回 dict：{full_profile, sentence_pattern, word_usage, rhythm, emotion_expression}。
    """
    cleaned = [_truncate(t) for t in corpus_texts if t and t.strip()][:MAX_CORPUS]
    if not cleaned:
        raise llm_mod.LLMError("没有可提炼的语料")

    corpus_block = "\n".join(f"- {t}" for t in cleaned)
    if existing_profile:
        user_content = (
            f"以下是该角色已有的说话风格档案（约{len(existing_profile)}字）：\n"
            f"{existing_profile}\n\n"
            f"以下是该角色新增的说话语料：\n{corpus_block}\n\n"
            "请融合新语料反映的特征，提炼一份更新后的说话风格档案（保持约500字，不要膨胀）。"
        )
    else:
        user_content = f"以下是该角色的说话语料：\n{corpus_block}"

    msgs = [
        llm_mod.LLMMessage(role="system", content=_SYSTEM_PROMPT),
        llm_mod.LLMMessage(role="user", content=user_content),
    ]
    result = llm.chat_structured(msgs, SPEECH_STYLE_TOOL)

    # 确保 full_profile 不超 ~500 字（略放宽到 550，允许截断）
    fp = result.get("full_profile", "")
    if len(fp) > 550:
        result["full_profile"] = fp[:550]
    return result
