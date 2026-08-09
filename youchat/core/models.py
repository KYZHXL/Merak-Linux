"""数据模型：角色、成员、社交画像、事件记忆、关键词钩子。

全部记忆按 character_id 隔离，是"多角色可切换 + 同群多 AI 互聊"的基础。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


def _now() -> float:
    return time.time()


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


@dataclass
class Taboo:
    """一条禁忌：text 是规则表述，keywords 是同义词（L1），examples 是"也算违规"的示例（L2）。"""

    text: str
    keywords: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @classmethod
    def from_value(cls, value) -> "Taboo":
        """兼容 dict（{text, keywords, examples}）与纯字符串两种定义格式。"""
        if isinstance(value, str):
            return cls(text=value)
        if isinstance(value, dict):
            return cls(
                text=str(value.get("text", "")),
                keywords=[str(k) for k in value.get("keywords", [])],
                examples=[str(e) for e in value.get("examples", [])],
            )
        raise ValueError(f"taboo 定义格式不支持: {value!r}")


@dataclass
class Character:
    """静态核心（Persona Core）：不可变的人设与世界观约束。"""

    character_id: str
    name: str
    personality: str            # 性格，如"外冷内热的吐槽役"
    speech_style: str           # 说话风格，如"短句、爱用反问、偶尔毒舌"
    background: str             # 背景故事
    worldview: str              # 世界观约束，如"绝不承认自己熬夜"
    taboos: list[Taboo] = field(default_factory=list)   # 绝对不可违背的底线
    reference_speech: str = ""  # 参考说话文本（该角色的台词/对话样本），用于提炼说话方式

    def to_prompt(self) -> str:
        parts = [
            f"角色名：{self.name}",
            f"性格：{self.personality}",
            f"说话风格：{self.speech_style}",
            f"背景：{self.background}",
            f"世界观：{self.worldview}",
        ]
        if self.taboos:
            parts.append("不可逾越的底线：" + "；".join(t.text for t in self.taboos))
        return "\n".join(parts)


@dataclass
class Member:
    """群成员：机器人对每个群成员的基础认知（跨角色共享）。"""

    member_id: str
    display_name: str
    note: str = ""              # 机器人对成员的备注，如"爱发猫图的家伙"


@dataclass
class SocialProfile:
    """社交演化层：角色对某个成员的动态认知。全部字段可随对话演化。"""

    character_id: str
    member_id: str
    affinity: int = 0           # 好感度 -100..100，随互动演化
    nickname: str = ""          # 角色对该成员的称呼
    interaction_style: str = "" # 角色对该成员的互动风格，如"爱损他"
    notes: list[str] = field(default_factory=list)  # 零散观察
    updated_at: float = field(default_factory=_now)

    def affinity_label(self) -> str:
        if self.affinity >= 30:
            return "亲近"
        if self.affinity <= -30:
            return "疏远"
        if self.affinity >= 5:
            return "友善"
        if self.affinity <= -5:
            return "冷淡"
        return "普通"


@dataclass
class MemoryEntry:
    """事件记忆层：可检索的记忆条目，带 keyword hooks 作为检索主轴。"""

    entry_id: str
    character_id: str
    summary: str                # 事件摘要
    hooks: list[str] = field(default_factory=list)   # 重点词钩子
    participants: list[str] = field(default_factory=list)  # 参与者 member_id
    sentiment: Sentiment = Sentiment.NEUTRAL
    created_at: float = field(default_factory=_now)
    last_accessed_at: float = field(default_factory=_now)
    access_count: int = 0
    source_round: Optional[int] = None   # 产生该记忆的对话轮次

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = _now()


@dataclass
class Event:
    """从对话提取出的结构化事件（compaction 产出）。"""

    summary: str
    hooks: list[str]
    participants: list[str]
    sentiment: Sentiment = Sentiment.NEUTRAL


@dataclass
class SocialUpdate:
    """对某成员社交画像的更新（compaction 产出）。

    LLM 返回的是成员显示名（member_name），调用方负责反查到 member_id。
    """

    member_id: str = ""
    member_name: str = ""
    affinity_delta: int = 0
    nickname: Optional[str] = None
    interaction_style: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ChatMessage:
    """一条聊天消息（抽象接口层）。scene 区分群聊/私聊。"""

    character_id: str
    sender_id: str
    sender_name: str
    text: str
    scene: str = "group"   # "group" 群聊 | "private" 私聊
    round: int = 0
    timestamp: float = field(default_factory=_now)

    @property
    def is_system(self) -> bool:
        return self.sender_id == "SYSTEM"
