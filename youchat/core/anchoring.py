"""人设锚定：上下文组装 + 一致性过滤。

每次生成回复时，组装上下文：
  静态核心（完整，不可变）→ 社交演化层（与当前说话者相关）→ 事件记忆（hooks 召回）→ 近期上下文

一致性过滤：演化记忆（社交画像/事件）里如果出现与核心人设冲突的内容
（触碰 taboos / 与世界观明显相悖），在注入前拦截，防止记忆污染核心人设。
"""
from __future__ import annotations

from .models import Character, MemoryEntry, SocialProfile
from .storage import Storage
from .. import llm as llm_mod


def filter_consistent_entries(
    character: Character,
    entries: list[MemoryEntry],
    checker=None,
) -> list[MemoryEntry]:
    """过滤掉与核心人设冲突的事件记忆。

    用三层阶梯式 checker（core/consistency.py）判定。checker 缺省时从
    角色 taboo 现场构造纯规则版（L1/L2，不调 LLM），保持旧接口兼容。
    """
    if not character.taboos:
        return entries
    if checker is None:
        from .consistency import TabooViolationChecker
        checker = TabooViolationChecker(
            character.taboos, llm=None, use_llm=False, character_name=character.name
        )
    return [e for e in entries if not checker.violates(e.summary, e.hooks)]


def build_context(
    character: Character,
    storage: Storage,
    current_sender_id: str,
    recalled: list[tuple[MemoryEntry, float, list[str], str]],
    recent: list[dict],
    budget: "LayerBudget | None" = None,
    speech_profile: str = "",
    reply_strategy: str = "",
) -> list[llm_mod.LLMMessage]:
    """组装注入 LLM 的 system + 记忆 + 近期对话。

    recalled 元素为 (entry, score, matched_hooks, reason)，reason ∈ {"hook","semantic"}。
    budget 为空时不限预算（旧行为）；传入时用分层调度封顶 L2/L1。
    speech_profile：从语料提炼的说话风格档案（~500字）；reply_strategy：本轮回复策略引导。
    """
    from .scheduling import LayerBudget, schedule_layers

    budget = budget or LayerBudget()
    # 静态核心（完整）
    parts = [
        "【角色设定 · 不可违背】",
        character.to_prompt(),
    ]

    # 说话风格档案（从角色语料提炼，替代生硬的 speech_style）
    if speech_profile:
        parts.append("\n【你的说话方式】（从你的语料提炼）")
        parts.append(speech_profile)
    elif character.speech_style:
        parts.append("\n【你的说话方式】")
        parts.append(character.speech_style)

    # 社交演化层：当前说话者的画像
    profile = storage.get_social_profile(character.character_id, current_sender_id)
    if profile:
        parts.append("\n【对该成员的关系】")
        parts.append(
            f"你们的关系：{profile.affinity_label()}（好感度 {profile.affinity:+d}）"
        )
        if profile.nickname:
            parts.append(f"你称呼他/她：{profile.nickname}")
        if profile.interaction_style:
            parts.append(f"你的互动风格：{profile.interaction_style}")
        if profile.notes:
            parts.append("你的观察备注：" + "；".join(profile.notes[-3:]))
    else:
        parts.append("\n【对该成员的关系】")
        parts.append("你们还不熟，保持礼貌而自然。" )

    # 事件记忆（hook 召回 + 语义召回，经一致性过滤，按预算截断）
    if recalled:
        scheduled = schedule_layers("", recalled, [], budget)
        parts.append("\n【你想起来的往事】")
        for entry, score, matched, reason, summary in scheduled["long_term"]:
            if matched:
                m = "、".join(matched)
                parts.append(f"- {summary}（想起关键词：{m}）")
            else:
                parts.append(f"- {summary}（隐约觉得相关）")

    system_text = "\n".join(parts)
    system_msg = llm_mod.LLMMessage(role="system", content=system_text)

    # 近期上下文 + 口语化约束 + 回复策略
    dialogue = [
        llm_mod.LLMMessage(
            role="system",
            content=(
                "聊天要求：像真人一样说话，别像客服。允许不完整句、省略号、语气词、说半句；"
                "不要总结、不要列点、不必每条都回答完整；可以只回一句、反问、岔开话题。"
            ),
        ),
    ]
    if reply_strategy:
        dialogue.append(llm_mod.LLMMessage(role="system", content=f"本轮打算：{reply_strategy}"))
    scheduled_recent = schedule_layers("", [], recent, budget)["short_term"]
    for m in scheduled_recent:
        if m["sender_id"] == character.character_id:
            dialogue.append(
                llm_mod.LLMMessage(role="assistant", content=m["text"])
            )
        else:
            dialogue.append(
                llm_mod.LLMMessage(
                    role="user",
                    content=m["text"],
                    name=m["sender_name"],
                )
            )
    return [system_msg] + dialogue
