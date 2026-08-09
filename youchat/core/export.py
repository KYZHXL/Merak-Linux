"""角色记忆沉淀库导出：每角色一个 txt 文件。

内容三层：事件记忆 / 社交画像 / 近期对话。UTF-8、人类可读、体积小。
由引擎在 compaction 后自动调用；也可脚本手动触发。
"""
from __future__ import annotations

import time
from pathlib import Path

from .models import Character, MemoryEntry, SocialProfile
from .storage import Storage


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _member_name(storage: Storage, member_id: str) -> str:
    m = storage.get_member(member_id)
    return m.display_name if m else member_id


def _render_memories(storage: Storage, character_id: str) -> str:
    entries: list[MemoryEntry] = sorted(
        storage.all_memory_entries(character_id),
        key=lambda e: e.created_at, reverse=True,
    )
    if not entries:
        return "（暂无沉淀记忆）\n"
    lines = []
    for i, e in enumerate(entries, 1):
        participants = "、".join(_member_name(storage, p) for p in e.participants) if e.participants else ""
        meta = f"hooks: {', '.join(e.hooks)}"
        if participants:
            meta += f" | 相关: {participants}"
        meta += f" | 情感: {e.sentiment.value} | 想起: {e.access_count} 次"
        lines.append(f"{i}. {e.summary}")
        lines.append(f"   {meta}")
    return "\n".join(lines) + "\n"


def _render_profiles(storage: Storage, character_id: str) -> str:
    profiles: list[SocialProfile] = sorted(
        storage.all_social_profiles(character_id),
        key=lambda p: p.affinity, reverse=True,
    )
    if not profiles:
        return "（暂无社交关系）\n"
    lines = []
    for p in profiles:
        name = _member_name(storage, p.member_id)
        extra = f" 称呼: {p.nickname}" if p.nickname else ""
        style = f" 互动: {p.interaction_style}" if p.interaction_style else ""
        lines.append(f"- {name}: 好感 {p.affinity:+d}（{p.affinity_label()}）{extra}{style}")
        for note in p.notes[-3:]:
            lines.append(f"  备注: {note}")
    return "\n".join(lines) + "\n"


def _render_recent(storage: Storage, character_id: str, character: Character, limit: int = 20) -> str:
    msgs = storage.recent_messages(character_id, limit=limit)
    if not msgs:
        return "（暂无近期对话）\n"
    lines = [f"[{m['sender_name']}] {m['text']}" for m in msgs]
    return "\n".join(lines) + "\n"


def export_memory_txt(character: Character, storage: Storage, out_path: Path,
                      recent_limit: int = 20) -> None:
    """把角色全部记忆沉淀成可读 txt。out_path 父目录自动创建。"""
    cid = character.character_id
    parts = [
        f"# 角色记忆沉淀库：{character.name} ({cid})",
        f"# 导出时间：{_fmt_time(time.time())}",
        "",
        f"## 事件记忆 ({len(storage.all_memory_entries(cid))} 条)",
        _render_memories(storage, cid),
        "",
        f"## 社交画像 ({len(storage.all_social_profiles(cid))} 人)",
        _render_profiles(storage, cid),
        "",
        f"## 近期对话 (最近 {recent_limit} 条)",
        _render_recent(storage, cid, character, limit=recent_limit),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts), encoding="utf-8")
