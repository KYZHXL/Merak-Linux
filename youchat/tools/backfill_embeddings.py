"""给已有记忆批量补向量（换 embedding 模型或迁移旧库时运行）。

用法：python -m youchat.tools.backfill_embeddings [--config config.yaml]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from youchat.core.embeddings import get_embedding_provider
from youchat.core.storage import Storage
from youchat.core.vector_index import VectorIndex


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    cfg_path = project_root / "config.yaml"
    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    db_path = project_root / config["storage"]["db_path"]

    provider = get_embedding_provider(config)
    if provider is None:
        print("未配置 embedding_model，无需 backfill。")
        return 0

    storage = Storage(db_path)
    vindex = VectorIndex(storage, provider)
    characters = storage._conn.execute("SELECT DISTINCT character_id FROM memory_entries").fetchall()
    total = 0
    for (cid,) in characters:
        entries = storage.all_memory_entries(cid)
        missing = [(e.entry_id, cid, e.summary + " " + " ".join(e.hooks))
                   for e in entries if not storage.has_embedding(e.entry_id)]
        if missing:
            print(f"角色 {cid}: 补 {len(missing)} 条向量")
            vindex.add_batch(missing)
            total += len(missing)
    print(f"完成，共补 {total} 条向量。")
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
