"""分层记忆调度：L3 核心 / L2 长期 / L1 短期，预算封顶。

超长对话（1000/10000 轮）时，记忆 + 人设 + 对话塞进上下文会爆窗口。
本模块用预算分配把注入量封死：
- L3 核心（人设 + 当前说话者画像）：恒完整注入，不可压缩
- L2 长期（召回记忆）：按 score 截断，每条限长
- L1 短期（近期对话）：从最旧开始丢弃，最近 N 条保完整
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LayerBudget:
    core: int = 900       # 静态人设 + 画像（不可压缩）
    long_term: int = 1000 # 召回记忆总预算
    short_term: int = 1500# 近期对话总预算
    per_entry_chars: int = 120  # 单条记忆摘要限长

    @classmethod
    def from_config(cls, cfg: dict | None) -> "LayerBudget":
        cfg = cfg or {}
        return cls(
            core=int(cfg.get("core", 900)),
            long_term=int(cfg.get("long_term", 1000)),
            short_term=int(cfg.get("short_term", 1500)),
            per_entry_chars=int(cfg.get("per_entry_chars", 120)),
        )


def estimate_chars(text: str) -> int:
    """粗略字符数估算（无 tokenizer，够用于预算控制）。"""
    return len(text)


def schedule_layers(
    core_text: str,
    recalled: list[tuple[object, float, list[str], str]],
    recent: list[dict],
    budget: LayerBudget,
) -> dict:
    """按预算裁剪三层，返回 {"core": str, "long_term": list, "short_term": list}。

    recalled 元素: (entry, score, matched, reason)；recent 元素: {"text", "sender_name", ...}
    """
    # L3 核心：超预算时仍保留（不可压缩），但截断到预算上限避免溢出
    core_out = core_text
    if len(core_out) > budget.core:
        core_out = core_out[:budget.core]

    # L2 长期：按 score 降序，逐条塞直到超预算
    long_term_out: list = []
    used = 0
    for entry, score, matched, reason in sorted(recalled, key=lambda x: x[1], reverse=True):
        summary = entry.summary[: budget.per_entry_chars]
        cost = estimate_chars(summary) + 20
        if used + cost > budget.long_term and long_term_out:
            break
        long_term_out.append((entry, score, matched, reason, summary))
        used += cost

    # L1 短期：从最近往回，超预算丢弃更旧的
    short_term_out: list[dict] = []
    used = 0
    for m in reversed(recent):  # 从最新开始
        cost = estimate_chars(m.get("text", "")) + 20
        if used + cost > budget.short_term and short_term_out:
            break
        short_term_out.insert(0, m)  # 保持时间正序
        used += cost

    return {
        "core": core_out,
        "long_term": long_term_out,
        "short_term": short_term_out,
    }
