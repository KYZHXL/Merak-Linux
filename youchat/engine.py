"""聊天引擎：收消息 → 检索 → 锚定 → 生成 → 沉淀。

- 每条消息先做 hooks 抽取，匹配历史记忆，注入上下文后生成回复
- 每轮把消息写入近期上下文缓冲；超过窗口后触发结构化沉淀（compaction）
- 沉淀产物：事件记忆 + 社交画像更新，全部按 character_id 隔离
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import llm as llm_mod
from .core.anchoring import build_context, filter_consistent_entries
from .core.extraction import compact_conversation, extract_message_hooks
from .core.models import (
    Character,
    ChatMessage,
    Member,
    MemoryEntry,
    SocialProfile,
)
from .core.persona import load_characters_dir
from .core.reply_strategy import analyze_reply_strategy, strategy_to_prompt
from .core.storage import Storage

log = logging.getLogger("youchat.engine")


@dataclass
class RoundResult:
    """一轮消息处理的结果，供测试壳/验证脚本核验。"""

    round: int
    recalled: list[dict] = field(default_factory=list)  # 召回的记忆（含命中关键词/语义命中）
    reply: str = ""
    compacted: bool = False
    social_updates: list[dict] = field(default_factory=list)
    new_events: list[dict] = field(default_factory=list)


class YouChatEngine:
    def __init__(
        self,
        storage: Storage,
        llm: "llm_mod.LLMClient",
        characters: dict[str, Character],
        config: dict,
    ):
        self.storage = storage
        self.llm = llm
        self.characters = characters
        self.cfg = config.get("engine", {})
        self.buffer_size = int(self.cfg.get("episodic_buffer_size", 24))
        self.compact_after = int(self.cfg.get("compact_after", 24))
        self.max_recalled = int(self.cfg.get("max_recalled_memories", 5))

        # 检索器：provider=None 时退化为纯 hook 模式
        from .core.embeddings import get_embedding_provider
        from .core.retrieval import Retriever

        self.retriever = Retriever(
            storage,
            provider=get_embedding_provider(config),
            cfg=config.get("retrieval", {}),
        )

        # 串行化 handle_message：QQ 后台线程与 UI 可能并发调用，
        # SQLite 单连接 + 引擎状态需互斥
        self._msg_lock = threading.RLock()
        self._speech_profiles: dict[str, str] = {}

        from .core.scheduling import LayerBudget
        self.budget = LayerBudget.from_config(config.get("context_budget", {}))

        # 三层阶梯式 taboo 检查（默认开；config.consistency.checker: off 关闭）
        from .core.consistency import build_checker

        mode = config.get("consistency", {}).get("checker", "auto")
        self.checkers: dict[str, object] = {}
        for cid, ch in characters.items():
            checker = build_checker(ch, llm=llm, mode=mode)
            if checker:
                self.checkers[cid] = checker

        # 供模拟群聊做"发言人 → 成员"反查
        self._member_name_to_id: dict[str, str] = {}

    # ---- 成员管理 ----

    def ensure_member(self, member_id: str, display_name: str) -> None:
        with self._msg_lock:
            member = self.storage.get_member(member_id)
            if member is None:
                self.storage.upsert_member(Member(member_id=member_id, display_name=display_name))
            elif member.display_name != display_name:
                self.storage.upsert_member(Member(member_id=member_id, display_name=display_name))
            self._member_name_to_id[display_name] = member_id

    def resolve_member_id(self, display_name: str) -> str:
        """按显示名反查 member_id；查不到则按名字本身注册一个。"""
        with self._msg_lock:
            if display_name in self._member_name_to_id:
                return self._member_name_to_id[display_name]
            mid = f"u-{display_name}"
            self.ensure_member(mid, display_name)
            return mid

    # ---- 单条消息处理 ----

    def handle_message(self, message: ChatMessage) -> RoundResult:
        with self._msg_lock:
            return self._handle_message_locked(message)

    def _handle_message_locked(self, message: ChatMessage) -> RoundResult:
        character = self.characters[message.character_id]
        self.ensure_member(message.sender_id, message.sender_name)

        round_no = self.storage.next_seq()
        result = RoundResult(round=round_no)

        # 1. hooks 抽取 + 记忆召回（召回后做一致性过滤，剔除冲突记忆）
        query_hooks = extract_message_hooks(self.llm, message.text)
        recalled = self.retriever.retrieve(
            message.character_id, query_hooks,
            query_text=message.text, limit=self.max_recalled,
        )
        recalled = [
            (e, score, matched, reason)
            for e, score, matched, reason in recalled
            if filter_consistent_entries(character, [e], checker=self.checkers.get(message.character_id))
        ]
        for entry, score, matched, reason in recalled:
            result.recalled.append(
                {"summary": entry.summary, "hooks": entry.hooks, "matched": matched,
                 "score": round(score, 2), "reason": reason}
            )
            self.storage.touch_memory(entry.entry_id)

        # 2. 组装上下文并生成（含回复策略分析 + 说话风格档案）
        recent = self.storage.recent_messages(message.character_id, limit=self.buffer_size)
        speech_profile = self._get_speech_profile(character, message.character_id)
        strategy = analyze_reply_strategy(
            self.llm, character.name, message.text,
            [m["text"] for m in recent],
        )
        strategy_prompt = strategy_to_prompt(strategy)
        messages = build_context(
            character, self.storage, message.sender_id, recalled, recent,
            budget=self.budget, speech_profile=speech_profile,
            reply_strategy=strategy_prompt,
        )
        messages.append(llm_mod.LLMMessage(role="user", content=message.text, name=message.sender_name))

        try:
            reply = self.llm.chat(messages)
        except llm_mod.LLMError as e:
            log.warning("生成失败，跳过本轮回复: %s", e)
            reply = ""
        result.reply = reply.strip()

        # 2b. 角色回复写入语料（自动积累说话方式）
        if result.reply:
            self.storage.add_corpus(message.character_id, result.reply)

        # 3. 写入近期上下文
        self.storage.append_message(
            message.character_id, message.sender_id,
            message.sender_name, message.text, message.timestamp,
        )
        if reply:
            self.storage.append_message(
                message.character_id, character.character_id,
                character.name, reply, time.time(),
            )

        # 4. 触发沉淀
        if round_no >= self.compact_after:
            result.compacted = self._run_compaction(character, message.character_id)
        else:
            result.compacted = False

        return result

    def _run_compaction(self, character: Character, character_id: str) -> bool:
        """把近期上下文沉淀成事件记忆 + 社交更新。"""
        recent = self.storage.recent_messages(character_id, limit=self.buffer_size * 2)
        if len(recent) < 4:
            return False

        system_prompt = (
            f"你是{character.name}。你的角色设定：\n{character.to_prompt()}\n\n"
            "你正在把群聊里的经历沉淀成记忆。"
        )
        try:
            events, social_updates = compact_conversation(
                self.llm, system_prompt, recent, character.name
            )
        except llm_mod.LLMError as e:
            log.warning("沉淀失败: %s", e)
            return False

        # 事件记忆入库（经一致性过滤 + 近邻去重）
        # 去重候选来自向量近邻 top-20（而非全量两两比对），O(events×N) → O(events×20)
        new_entries: list[MemoryEntry] = []
        texts: list[str] = []
        for ev in events:
            entry = MemoryEntry(
                entry_id="",
                character_id=character_id,
                summary=ev.summary,
                hooks=ev.hooks,
                participants=[self.resolve_member_id(p) for p in ev.participants],
                sentiment=ev.sentiment,
                source_round=max((m["seq"] for m in recent), default=None),
            )
            checker = self.checkers.get(character_id)
            if entry.summary and not self._conflicts_with_persona(character, entry):
                # 三层阶梯式 taboo 判定（L1/L2 规则 + L3 LLM 兜底）
                if checker and checker.check_entry(entry):
                    log.debug("taboo 拦截记忆: %s", entry.summary)
                    continue
                new_entries.append(entry)
                texts.append(entry.summary + " " + " ".join(entry.hooks))

        # 批量编码新事件（向量索引一次性）
        if self.retriever.vector_index:
            new_vecs = self.retriever.provider.encode(texts)
            for i, entry in enumerate(new_entries):
                dup = self._find_duplicate_near(character_id, new_vecs[i], entry)
                if dup is not None:
                    dup.hooks = list(dict.fromkeys(dup.hooks + entry.hooks))
                    dup.access_count += 1
                    self.storage.touch_memory(dup.entry_id)
                else:
                    eid = self.storage.add_memory_entry(entry)
                    self.retriever.vector_index.add(eid, character_id, texts[i])
        else:
            # 无向量模式：退化为全量两两比对（旧行为）
            existing = self.storage.all_memory_entries(character_id)
            for entry in new_entries:
                if self._is_duplicate(existing, entry):
                    for old in existing:
                        if _same_event(old, entry):
                            old.hooks = list(dict.fromkeys(old.hooks + entry.hooks))
                            old.access_count += 1
                            self.storage.touch_memory(old.entry_id)
                            break
                else:
                    self.storage.add_memory_entry(entry)
                    existing.append(entry)

        # 社交更新入库
        for upd in social_updates:
            member_id = self.resolve_member_id(upd.member_name)
            profile = self.storage.get_social_profile(character_id, member_id)
            if profile is None:
                profile = SocialProfile(
                    character_id=character_id,
                    member_id=member_id,
                )
            profile.affinity = max(-100, min(100, profile.affinity + upd.affinity_delta))
            if upd.nickname:
                profile.nickname = upd.nickname
            if upd.interaction_style:
                profile.interaction_style = upd.interaction_style
            if upd.note:
                profile.notes.append(upd.note)
                profile.notes = profile.notes[-10:]
            profile.updated_at = time.time()
            self.storage.save_social_profile(profile)

        # 清空缓冲前导出记忆沉淀库（txt），确保近期对话仍可读
        self._export_memory(character, character_id)
        # 清空缓冲，避免重复沉淀
        self.storage.clear_episodic_buffer(character_id)
        return True

    def _export_memory(self, character: Character, character_id: str) -> None:
        """把角色记忆沉淀成 memory/<character_id>.txt。失败仅记日志不阻塞。"""
        from .core.export import export_memory_txt
        from .runtime import resolve_repo_root

        out = resolve_repo_root() / "memory" / f"{character_id}.txt"
        try:
            export_memory_txt(character, self.storage, out)
            log.debug("已导出记忆沉淀库 %s", out)
        except OSError as e:
            log.warning("导出记忆沉淀库失败 %s: %s", out, e)

    def _conflicts_with_persona(self, character: Character, entry: MemoryEntry) -> bool:
        return len(filter_consistent_entries(character, [entry])) == 0

    def _get_speech_profile(self, character: Character, character_id: str) -> str:
        """获取说话风格档案。

        优先用语料提炼（reference_speech + 自动积累的回话）；
        无语料时退回角色的 speech_style。
        用缓存避免每次重复提炼。
        """
        if self._speech_profiles.get(character_id) is not None:
            return self._speech_profiles[character_id]
        corpus = self.storage.get_corpus(character_id, limit=60)
        if character.reference_speech:
            corpus = [character.reference_speech] + corpus
        profile = ""
        if corpus:
            try:
                from .core.speech_style import extract_speech_style

                result = extract_speech_style(self.llm, corpus)
                profile = result.get("full_profile", "")
            except llm_mod.LLMError as e:
                log.warning("说话风格提炼失败: %s", e)
        if not profile:
            profile = character.speech_style
        self._speech_profiles[character_id] = profile
        return profile

    def _find_duplicate_near(self, character_id: str, query_vec, new_entry: MemoryEntry) -> Optional[MemoryEntry]:
        """向量近邻 top-20 里找重复事件，找不到返回 None。"""
        if not self.retriever.vector_index:
            return None
        candidates = self.retriever.vector_index.search(character_id, query_vec, k=20)
        for eid, _cos in candidates:
            old = self.storage.get_memory_entry(eid)
            if old is not None and _same_event(old, new_entry):
                return old
        return None

    def _is_duplicate(self, existing: list[MemoryEntry], entry: MemoryEntry) -> bool:
        return any(_same_event(old, entry) for old in existing)


def _same_event(a: MemoryEntry, b: MemoryEntry) -> bool:
    """判定两条记忆是否描述同一事件。

    排除成员名这类常见钩子后，剩余"事件性钩子"有重叠才算重复。
    例如 [阿伟, 修电脑] vs [阿伟, 修电脑] → 重复；[阿伟, 修电脑] vs [阿伟, 奶茶] → 不重复。
    """
    member_hooks = {"阿伟", "小美", "大壮", "老王", "老猫"}
    sa = {h.strip().lower() for h in a.hooks} - member_hooks
    sb = {h.strip().lower() for h in b.hooks} - member_hooks
    return bool(sa) and bool(sb) and len(sa & sb) >= 1
