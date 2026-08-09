"""内存余弦向量索引，向量持久化在 SQLite BLOB（单一存储，物理上不可能漂移）。

- 每个角色一个 [n, dim] float32 矩阵 + 并行 entry_ids 列表，按需懒加载
- 检索用 torch matmul 做余弦 top-k（10000 条 < 1ms）
- add() 编码文档（summary + hooks）→ 存 BLOB → 入内存矩阵
- 定义成接口：未来真到百万条可 drop-in 换 lancedb/HNSW
"""
from __future__ import annotations

import logging

import numpy as np

from .embeddings import EmbeddingProvider
from .storage import Storage

log = logging.getLogger("youchat.vector_index")


class VectorIndex:
    def __init__(self, storage: Storage, provider: EmbeddingProvider):
        self.storage = storage
        self.provider = provider
        self._matrices: dict[str, np.ndarray] = {}   # character_id -> [n, dim]
        self._ids: dict[str, list[str]] = {}         # character_id -> [entry_id]

    # ---- 加载 ----

    def ensure_loaded(self, character_id: str) -> bool:
        """懒加载该角色的向量矩阵。返回是否有向量数据。"""
        if character_id in self._matrices:
            return len(self._matrices[character_id]) > 0
        pairs = self.storage.get_embeddings(character_id)
        if not pairs:
            self._matrices[character_id] = np.zeros((0, self.provider.dim), dtype=np.float32)
            self._ids[character_id] = []
            return False
        ids, blobs = zip(*pairs)
        vecs = np.stack([np.frombuffer(b, dtype=np.float32) for b in blobs])
        self._matrices[character_id] = vecs
        self._ids[character_id] = list(ids)
        return True

    # ---- 写入 ----

    def add(self, entry_id: str, character_id: str, text: str) -> None:
        vec = self.provider.encode([text])[0]
        self.ensure_loaded(character_id)  # 先加载，避免把刚存的向量重复计入
        self.storage.save_embedding(entry_id, vec.astype(np.float32).tobytes())
        mat = self._matrices[character_id]
        # 增量追加
        self._matrices[character_id] = np.vstack([mat, vec.astype(np.float32)]) if len(mat) else vec.astype(np.float32)[None, :]
        self._ids[character_id].append(entry_id)

    def add_batch(self, items: list[tuple[str, str, str]], batch_size: int = 32) -> None:
        """批量添加 (entry_id, character_id, text)，按角色分批编码。"""
        by_char: dict[str, list[tuple[str, str]]] = {}
        for eid, cid, text in items:
            by_char.setdefault(cid, []).append((eid, text))
        for cid, pairs in by_char.items():
            self.ensure_loaded(cid)  # 先加载，避免把刚存的向量重复计入
            vecs = self.provider.encode([t for _, t in pairs])
            vecs = vecs.astype(np.float32)
            for i, (eid, _) in enumerate(pairs):
                self.storage.save_embedding(eid, vecs[i].tobytes())
                mat = self._matrices[cid]
                self._matrices[cid] = np.vstack([mat, vecs[i]]) if len(mat) else vecs[i][None, :]
                self._ids[cid].append(eid)

    # ---- 检索 ----

    def search(self, character_id: str, query_vec: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        """余弦 top-k，返回 [(entry_id, cosine)]，cosine 越大越相关。"""
        if not self.ensure_loaded(character_id):
            return []
        mat = self._matrices[character_id]
        if len(mat) == 0:
            return []
        q = query_vec.reshape(1, -1).astype(np.float32)
        # torch matmul，向量已 L2 归一化 → 点积即余弦
        import torch

        scores = (torch.from_numpy(mat) @ torch.from_numpy(q).reshape(-1, 1)).numpy().flatten()
        k = min(k, len(scores))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [(self._ids[character_id][i], float(scores[i])) for i in idx]
