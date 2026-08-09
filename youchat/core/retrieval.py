"""关键词钩子检索：可解释的记忆召回主轴 + 向量语义兜底。

策略（Retriever 融合）：
- A 路（hook 主轴）：倒排索引（hook → entry_id 集合）取候选，只对候选做 score_entry。
  10k 条 O(N) 全表扫描 → O(#query_hooks × 倒排长度)。matched_hooks 照旧，可解释。
- B 路（向量语义兜底）：query_text 编码 → 余弦 top-k。文档向量 = summary + hooks（存事件语义），
  修复变体说法（"上次那台电脑" ↔ "修电脑"）召回。
- 融合：A 路排前（hook 主轴），B 路排后且限 semantic_slots 个。

provider=None（纯 hook 模式）时行为与升级前完全一致——14 项验证兼容红线。
"""
from __future__ import annotations

import time
from typing import Optional

from .embeddings import EmbeddingProvider
from .models import MemoryEntry
from .storage import Storage
from .vector_index import VectorIndex


def _normalize(hook: str) -> str:
    return hook.strip().lower()


def _jaccard_char(a: str, b: str) -> float:
    """字符级 Jaccard 相似度，仅无向量时作兜底。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def score_entry(entry: MemoryEntry, query_hooks: list[str], now: float,
                use_fuzzy: bool = True) -> tuple[float, list[str]]:
    """计算一条记忆与当前 hooks 的匹配分数（纯 hook 打分，兼容旧语义）。

    只有命中（精确钩子或字符相似）才给"召回资格分"；近期性/热度仅作
    命中记忆之间的排序加分，不能单独触发召回——避免"热门但无关"的记忆浮上来。
    use_fuzzy=False 时关闭字符 Jaccard 兜底（向量启用时由语义召回接管）。
    返回 (score, matched_hooks)。
    """
    entry_hooks = [_normalize(h) for h in entry.hooks]
    query = [_normalize(h) for h in query_hooks]

    matched = []
    for q in query:
        for e in entry_hooks:
            if e and (e == q or e in q or q in e):
                matched.append(q)
                break

    fuzzy = 0.0
    if use_fuzzy:
        for q in query:
            best = max((_jaccard_char(q, e) for e in entry_hooks if e), default=0.0)
            if best > 0.5:
                fuzzy = max(fuzzy, best)

    if not matched and fuzzy <= 0:
        return 0.0, []

    score = 3.0 * len(matched) + fuzzy * 1.0
    age_hours = (now - entry.created_at) / 3600.0
    score += max(0.0, 1.0 - age_hours / 720.0) * 0.5
    score += min(entry.access_count, 10) * 0.1
    return score, matched


def retrieve_memories(
    storage: Storage,
    character_id: str,
    query_hooks: list[str],
    limit: int = 5,
    now: float | None = None,
) -> list[tuple[MemoryEntry, float, list[str]]]:
    """纯 hook 检索（无向量模式），与升级前行为一致。返回 (entry, score, matched_hooks)。"""
    now = now or time.time()
    entries = storage.all_memory_entries(character_id)
    scored = []
    for e in entries:
        score, matched = score_entry(e, query_hooks, now)
        if score > 0:
            scored.append((e, score, matched))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


class _HookInvertedIndex:
    """hook → entry_id 集合的倒排索引，构建一次供多次检索复用。"""

    def __init__(self, storage: Storage, character_id: str):
        self._postings: dict[str, set[str]] = {}
        self._entries: dict[str, MemoryEntry] = {}
        for e in storage.all_memory_entries(character_id):
            self._entries[e.entry_id] = e
            for h in e.hooks:
                self._postings.setdefault(_normalize(h), set()).add(e.entry_id)

    def candidate_ids(self, query_hooks: list[str]) -> set[str]:
        ids: set[str] = set()
        for q in (_normalize(h) for h in query_hooks):
            if q in self._postings:
                ids |= self._postings[q]
        return ids

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        return self._entries.get(entry_id)


class Retriever:
    """融合检索：hook 主轴 + 向量语义兜底。provider=None 时退化为纯 hook。"""

    def __init__(self, storage: Storage, provider: Optional[EmbeddingProvider] = None, cfg: dict | None = None):
        self.storage = storage
        self.provider = provider
        self.cfg = cfg or {}
        self.vector_index = VectorIndex(storage, provider) if provider else None
        self._inverted: dict[str, _HookInvertedIndex] = {}
        self._threshold = float(self.cfg.get("vector_threshold", 0.45))
        self._semantic_slots = int(self.cfg.get("semantic_slots", 2))
        self._use_fuzzy = bool(self.cfg.get("use_fuzzy_fallback", provider is None))

    def _inverted_index(self, character_id: str) -> _HookInvertedIndex:
        if character_id not in self._inverted:
            self._inverted[character_id] = _HookInvertedIndex(self.storage, character_id)
        return self._inverted[character_id]

    def retrieve(
        self,
        character_id: str,
        query_hooks: list[str],
        query_text: str = "",
        limit: int = 5,
        now: float | None = None,
    ) -> list[tuple[MemoryEntry, float, list[str], str]]:
        """返回 (entry, score, matched_hooks, reason)，reason ∈ {"hook","semantic"}。"""
        now = now or time.time()
        if self.provider is None:
            # 纯 hook 模式：完全兼容旧行为（含 Jaccard 兜底）
            results = retrieve_memories(self.storage, character_id, query_hooks, limit, now)
            return [(e, s, m, "hook") for e, s, m in results]

        idx = self._inverted_index(character_id)
        candidate_ids = idx.candidate_ids(query_hooks)

        # A 路：hook 命中候选打分
        hook_results: list[tuple[MemoryEntry, float, list[str]]] = []
        for eid in candidate_ids:
            entry = idx.get(eid)
            if entry is None:
                continue
            score, matched = score_entry(entry, query_hooks, now, use_fuzzy=self._use_fuzzy)
            if score > 0:
                hook_results.append((entry, score, matched))
        hook_results.sort(key=lambda x: x[1], reverse=True)

        # B 路：向量语义兜底（用 query_text，变体说法召回）
        semantic_results: list[tuple[MemoryEntry, float, list[str]]] = []
        if self.vector_index and (query_text or query_hooks):
            query_vec = self.provider.encode([query_text or " ".join(query_hooks)])[0]
            candidates = self.vector_index.search(character_id, query_vec, k=limit * 3)
            for eid, cosine in candidates:
                if cosine < self._threshold:
                    continue
                # 优先用倒排缓存取条目，避免逐条查库
                entry = idx.get(eid) or self.storage.get_memory_entry(eid)
                if entry is None:
                    continue
                # 已被 A 路覆盖的跳过（A 路优先）
                if any(r[0].entry_id == eid for r in hook_results):
                    continue
                semantic_results.append((entry, cosine, []))

        # 融合：A 路全量在前，B 路取 semantic_slots 个垫后
        fused: list[tuple[MemoryEntry, float, list[str], str]] = []
        fused += [(e, s, m, "hook") for e, s, m in hook_results]
        fused += [(e, s, m, "semantic") for e, s, m in semantic_results[: self._semantic_slots]]
        return fused[:limit]
