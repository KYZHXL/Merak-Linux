"""Embedding 抽象层。

- EmbeddingProvider：统一的向量接口（encode 返回 L2 归一化的 float32 矩阵）
- LocalBgeEmbeddingProvider：本地 bge-small-zh 等模型（默认，零网络依赖，隐私好）
- OpenAICompatEmbeddingProvider：云端 embedding API（通义/Kimi 等，走 httpx 直连）
- get_embedding_provider：工厂。返回 None = 纯 hook 模式，现有行为完全不变

provider=None 是安全开关：不配置 embedding 时系统退化为纯关键词钩子检索。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import numpy as np

from .. import llm as llm_mod

log = logging.getLogger("youchat.embeddings")


class EmbeddingProvider(ABC):
    """向量提供者。encode 返回 [n, dim] float32 且已 L2 归一化的矩阵。"""

    dim: int

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        ...


class LocalBgeEmbeddingProvider(EmbeddingProvider):
    """本地 bge 系列模型（transformers），默认 BAAI/bge-small-zh-v1.5（512 维）。

    模块级单例懒加载：首次 encode 才载入模型（约 1-2s），不拖慢 engine 启动。
    """

    _singleton = None

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5", dim: int | None = None,
                 query_instruction: str = ""):
        self.model_name = model_name
        self._query_instruction = query_instruction or "为这个句子生成表示以用于检索相关文章："
        self.dim = dim or 512

    def _load(self):
        if LocalBgeEmbeddingProvider._singleton is None:
            import torch
            from transformers import AutoModel, AutoTokenizer

            log.info("加载本地 embedding 模型 %s ...", self.model_name)
            tok = AutoTokenizer.from_pretrained(self.model_name)
            model = AutoModel.from_pretrained(self.model_name)
            model.eval()
            self.dim = model.config.hidden_size or self.dim
            LocalBgeEmbeddingProvider._singleton = (tok, model)
            log.info("本地 embedding 模型加载完成，dim=%d", self.dim)
        return LocalBgeEmbeddingProvider._singleton

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        import torch

        tok, model = self._load()
        inputs = tok(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            outputs = model(**inputs)
        last_hidden = outputs.last_hidden_state
        mask = inputs["attention_mask"].unsqueeze(-1).to(last_hidden.dtype)
        summed = (last_hidden * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        vecs = summed / counts  # mean pooling
        norms = vecs.norm(dim=1, keepdim=True).clamp(min=1e-9)
        vecs = vecs / norms     # L2 归一化
        return vecs.numpy().astype(np.float32)


class OpenAICompatEmbeddingProvider(EmbeddingProvider):
    """云端 OpenAI 兼容 embedding（httpx 直连，不依赖 openai SDK）。

    兼容通义 text-embedding-v3、Kimi embeddings 等；输入按 32 条分块。
    """

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        import httpx

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        all_vecs = []
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for i in range(0, len(texts), 32):
            batch = texts[i : i + 32]
            payload = {"model": self.model, "input": batch}
            with httpx.Client(timeout=60) as client:
                r = client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
            if r.status_code >= 400:
                raise llm_mod.LLMError(f"Embedding API 错误 {r.status_code}: {r.text[:300]}")
            data = r.json()
            # 按顺序对齐 data[i].embedding
            vecs = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
            all_vecs.extend(vecs)
        arr = np.array(all_vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norms, 1e-9, None)
        return arr


def get_embedding_provider(config: dict) -> EmbeddingProvider | None:
    """工厂。config.model.embedding_model 为空 → 返回 None（纯 hook 模式）。

    取值规则：
    - ""                     → None（默认，纯 hook 检索）
    - "local:xxx"            → 本地 bge 模型，如 "local:BAAI/bge-base-zh-v1.5"
    - "local:ollama:xxx"     → Ollama 本地 embedding 模型，如 "local:ollama:nomic-embed-text"
    - 其他字符串              → 云端模型名，需配合 model.base_url / api_key / embedding_dim
    """
    emb_cfg = config.get("model", {})
    model = emb_cfg.get("embedding_model", "")
    if not model:
        return None
    if model.startswith("local:ollama:"):
        from .ollama import OLLAMA_V1

        return OpenAICompatEmbeddingProvider(
            base_url=OLLAMA_V1,
            api_key="",
            model=model.removeprefix("local:ollama:"),
            dim=int(emb_cfg.get("embedding_dim", 768)),
        )
    if model.startswith("local:"):
        name = model.removeprefix("local:")
        return LocalBgeEmbeddingProvider(name)
    dim = int(emb_cfg.get("embedding_dim", 1024))
    return OpenAICompatEmbeddingProvider(
        base_url=emb_cfg.get("base_url", ""),
        api_key=emb_cfg.get("api_key", ""),
        model=model,
        dim=dim,
    )
