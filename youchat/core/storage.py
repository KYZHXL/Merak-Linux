"""SQLite 存储层：结构化记忆 + 关键词钩子索引，全部按 character_id 隔离。

用内存态对象（models.py 的 dataclass）承载读写，SQLite 只做持久化。
向量索引（lancedb）是可选兜底，MVP 阶段由 retrieval 层在内存中完成近似匹配。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from .models import MemoryEntry, Member, SocialProfile, Sentiment


class Storage:
    def __init__(self, db_path: str | Path):
        import threading

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False 允许 QQ 后台线程访问；用锁串行化写
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                member_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS social_profiles (
                character_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                affinity INTEGER NOT NULL DEFAULT 0,
                nickname TEXT DEFAULT '',
                interaction_style TEXT DEFAULT '',
                notes TEXT DEFAULT '[]',
                updated_at REAL NOT NULL,
                PRIMARY KEY (character_id, member_id)
            );
            CREATE TABLE IF NOT EXISTS memory_entries (
                entry_id TEXT PRIMARY KEY,
                character_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                hooks TEXT NOT NULL DEFAULT '[]',
                participants TEXT NOT NULL DEFAULT '[]',
                sentiment TEXT NOT NULL DEFAULT 'neutral',
                created_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                source_round INTEGER,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS episodic_buffer (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS corpus (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_corpus_character ON corpus(character_id);
            CREATE INDEX IF NOT EXISTS idx_memory_character ON memory_entries(character_id);
            CREATE INDEX IF NOT EXISTS idx_social_character ON social_profiles(character_id);
            CREATE INDEX IF NOT EXISTS idx_buffer_character ON episodic_buffer(character_id);
            """
        )
        self._conn.commit()
        self._migrate_embedding_column()

    def _migrate_embedding_column(self) -> None:
        """对已存在的旧库幂等迁移：为 memory_entries 补 embedding 列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(memory_entries)").fetchall()}
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE memory_entries ADD COLUMN embedding BLOB")
            self._conn.commit()

    def get_member(self, member_id: str) -> Optional[Member]:
        row = self._conn.execute(
            "SELECT * FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        if row is None:
            return None
        return Member(member_id=row["member_id"], display_name=row["display_name"], note=row["note"])

    def upsert_member(self, member: Member) -> None:
        self._conn.execute(
            """
            INSERT INTO members(member_id, display_name, note) VALUES(?, ?, ?)
            ON CONFLICT(member_id) DO UPDATE SET
                display_name=excluded.display_name,
                note=excluded.note
            """,
            (member.member_id, member.display_name, member.note),
        )
        self._conn.commit()

    # ---- 社交画像 ----

    def get_social_profile(self, character_id: str, member_id: str) -> Optional[SocialProfile]:
        row = self._conn.execute(
            "SELECT * FROM social_profiles WHERE character_id = ? AND member_id = ?",
            (character_id, member_id),
        ).fetchone()
        if row is None:
            return None
        return SocialProfile(
            character_id=row["character_id"],
            member_id=row["member_id"],
            affinity=row["affinity"],
            nickname=row["nickname"],
            interaction_style=row["interaction_style"],
            notes=json.loads(row["notes"]),
            updated_at=row["updated_at"],
        )

    def save_social_profile(self, profile: SocialProfile) -> None:
        self._conn.execute(
            """
            INSERT INTO social_profiles(character_id, member_id, affinity, nickname,
                                        interaction_style, notes, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(character_id, member_id) DO UPDATE SET
                affinity=excluded.affinity,
                nickname=excluded.nickname,
                interaction_style=excluded.interaction_style,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                profile.character_id,
                profile.member_id,
                profile.affinity,
                profile.nickname,
                profile.interaction_style,
                json.dumps(profile.notes, ensure_ascii=False),
                profile.updated_at,
            ),
        )
        self._conn.commit()

    def all_social_profiles(self, character_id: str) -> list[SocialProfile]:
        rows = self._conn.execute(
            "SELECT * FROM social_profiles WHERE character_id = ?", (character_id,)
        ).fetchall()
        return [
            SocialProfile(
                character_id=r["character_id"],
                member_id=r["member_id"],
                affinity=r["affinity"],
                nickname=r["nickname"],
                interaction_style=r["interaction_style"],
                notes=json.loads(r["notes"]),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ---- 事件记忆 ----

    def add_memory_entry(self, entry: MemoryEntry) -> str:
        if not entry.entry_id:
            entry.entry_id = uuid.uuid4().hex[:12]
        sentiment = entry.sentiment
        if not isinstance(sentiment, Sentiment):
            sentiment = Sentiment(str(sentiment))
        self._conn.execute(
            """
            INSERT INTO memory_entries(entry_id, character_id, summary, hooks,
                                       participants, sentiment, created_at,
                                       last_accessed_at, access_count, source_round)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.character_id,
                entry.summary,
                json.dumps(entry.hooks, ensure_ascii=False),
                json.dumps(entry.participants, ensure_ascii=False),
                sentiment.value,
                entry.created_at,
                entry.last_accessed_at,
                entry.access_count,
                entry.source_round,
            ),
        )
        self._conn.commit()
        return entry.entry_id

    def get_memory_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def all_memory_entries(self, character_id: str) -> list[MemoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM memory_entries WHERE character_id = ?", (character_id,)
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ---- 向量持久化（embedding BLOB） ----

    def save_embedding(self, entry_id: str, vector: bytes) -> None:
        self._conn.execute(
            "UPDATE memory_entries SET embedding = ? WHERE entry_id = ?",
            (vector, entry_id),
        )
        self._conn.commit()

    def has_embedding(self, entry_id: str) -> bool:
        row = self._conn.execute(
            "SELECT embedding IS NOT NULL AS has FROM memory_entries WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return bool(row and row["has"])

    def get_embeddings(self, character_id: str) -> list[tuple[str, bytes]]:
        """返回该角色全部记忆的 (entry_id, 原始向量字节)。"""
        rows = self._conn.execute(
            "SELECT entry_id, embedding FROM memory_entries "
            "WHERE character_id = ? AND embedding IS NOT NULL",
            (character_id,),
        ).fetchall()
        return [(r["entry_id"], r["embedding"]) for r in rows]

    def touch_memory(self, entry_id: str) -> None:
        self._conn.execute(
            """
            UPDATE memory_entries
            SET last_accessed_at = ?, access_count = access_count + 1
            WHERE entry_id = ?
            """,
            (__import__("time").time(), entry_id),
        )
        self._conn.commit()

    def delete_memory_entry(self, entry_id: str) -> None:
        self._conn.execute("DELETE FROM memory_entries WHERE entry_id = ?", (entry_id,))
        self._conn.commit()

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            entry_id=row["entry_id"],
            character_id=row["character_id"],
            summary=row["summary"],
            hooks=json.loads(row["hooks"]),
            participants=json.loads(row["participants"]),
            sentiment=Sentiment(row["sentiment"]),
            created_at=row["created_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
            source_round=row["source_round"],
        )

    # ---- 近期上下文（episodic buffer） ----

    def append_message(self, character_id: str, sender_id: str,
                       sender_name: str, text: str, timestamp: float) -> None:
        self._conn.execute(
            """
            INSERT INTO episodic_buffer(character_id, sender_id, sender_name, text, timestamp)
            VALUES(?, ?, ?, ?, ?)
            """,
            (character_id, sender_id, sender_name, text, timestamp),
        )
        self._conn.commit()

    def recent_messages(self, character_id: str, limit: int = 40) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT * FROM episodic_buffer WHERE character_id = ?
            ORDER BY seq DESC LIMIT ?
            """,
            (character_id, limit),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "sender_id": r["sender_id"],
                "sender_name": r["sender_name"],
                "text": r["text"],
                "timestamp": r["timestamp"],
            }
            for r in reversed(rows)  # 时间正序返回
        ]

    def clear_episodic_buffer(self, character_id: str) -> None:
        self._conn.execute(
            "DELETE FROM episodic_buffer WHERE character_id = ?", (character_id,)
        )
        self._conn.commit()

    def next_seq(self) -> int:
        row = self._conn.execute("SELECT MAX(seq) AS m FROM episodic_buffer").fetchone()
        return (row["m"] or 0) + 1

    # ---- 说话风格语料（corpus）----

    def add_corpus(self, character_id: str, text: str, limit: int = 200) -> None:
        """记录一条角色回话作为语料。超出上限删最旧的。"""
        if not text or not text.strip():
            return
        self._conn.execute(
            "INSERT INTO corpus(character_id, text, created_at) VALUES(?, ?, ?)",
            (character_id, text.strip(), time.time()),
        )
        # 超上限删最旧
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM corpus WHERE character_id = ?", (character_id,)
        ).fetchone()
        if row["c"] > limit:
            self._conn.execute(
                "DELETE FROM corpus WHERE character_id = ? AND rowid IN ("
                "SELECT rowid FROM corpus WHERE character_id = ? ORDER BY rowid LIMIT ?)",
                (character_id, character_id, row["c"] - limit),
            )
        self._conn.commit()

    def get_corpus(self, character_id: str, limit: int = 100) -> list[str]:
        rows = self._conn.execute(
            "SELECT text FROM corpus WHERE character_id = ? ORDER BY rowid DESC LIMIT ?",
            (character_id, limit),
        ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def close(self) -> None:
        self._conn.close()
