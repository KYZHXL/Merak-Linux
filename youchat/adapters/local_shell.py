"""本地模拟群聊测试壳：不上真 QQ，验证记忆/人设内核。

用法（交互）：
    python -m youchat.adapters.local_shell --role laomao
    输入格式： `阿伟: 今天好冷`   —— 指定说话人发消息
               `!switch <role>`   —— 切换角色（验证多角色隔离）
               `!members`         —— 查看该角色对所有成员的好感度
               `!mem`             —— 查看该角色的历史记忆
               `!hooks <文本>`    —— 只看某消息会召回哪些记忆（不生成回复）
               `exit`             —— 退出

用法（脚本驱动，验证用）：
    python -m youchat.adapters.local_shell --role laomao --script demo.txt
    demo.txt 每行一条 `说话人: 内容`，自动逐条喂给引擎。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ..core.models import ChatMessage
from ..engine import YouChatEngine
from .base import Adapter


class LocalShell(Adapter):
    """模拟一个有多名成员的群聊。"""

    def __init__(self, engine: YouChatEngine, config: dict, default_role: str):
        super().__init__(engine)
        self.config = config
        self.role = default_role
        self.members = ["阿伟", "小美", "大壮", "老王"]

    def _send(self, sender: str, text: str):
        msg = ChatMessage(
            character_id=self.role,
            sender_id=self.engine.resolve_member_id(sender),
            sender_name=sender,
            text=text,
        )
        result = self.engine.handle_message(msg)
        print(f"\n  ✎ [{sender}] {text}")
        if result.recalled:
            descs = []
            for r in result.recalled:
                m = "、".join(r["matched"]) if r["matched"] else "语义相近"
                tag = "关键词" if r.get("reason") == "hook" else "语义"
                descs.append(f"{r['summary']}（{tag}:{m}）")
            print("  🧠 想起:", " | ".join(descs))
        if result.reply:
            print(f"  🤖 [{self.role}] {result.reply}")
        if result.compacted:
            print("  📦 触发记忆沉淀")
        for u in result.social_updates:
            print(f"  ⚖ 社交更新: {u}")
        return result

    def _handle_command(self, line: str) -> bool:
        parts = line.strip().split()
        cmd = parts[0]
        if cmd == "!switch":
            role = parts[1]
            if role in self.engine.characters:
                self.role = role
                print(f"已切换到角色: {role}")
            else:
                print(f"未知角色: {role}，可用: {list(self.engine.characters)}")
        elif cmd == "!members":
            for m in self.members:
                p = self.engine.storage.get_social_profile(self.role, self.engine.resolve_member_id(m))
                if p:
                    print(f"  {m}: 好感度 {p.affinity:+d}（{p.affinity_label()}）{(' 称呼:'+p.nickname) if p.nickname else ''}")
                else:
                    print(f"  {m}: 还没接触")
        elif cmd == "!mem":
            for e in self.engine.storage.all_memory_entries(self.role):
                print(f"  · {e.summary} [hooks: {','.join(e.hooks)}] {e.sentiment.value}")
        elif cmd == "!hooks":
            text = " ".join(parts[1:])
            from ..core.extraction import extract_message_hooks
            hooks = extract_message_hooks(self.engine.llm, text)
            print(f"  消息 hooks: {hooks}")
            for entry, score, matched, reason in self.engine.retriever.retrieve(
                self.role, hooks, query_text=text, limit=5
            ):
                tag = "关键词" if reason == "hook" else "语义"
                m = "、" .join(matched) if matched else "（语义相近）"
                print(f"  命中[{tag}]: {entry.summary} (score={score:.2f}, {m})")
        elif cmd == "!exit":
            return False
        else:
            print("未知命令，可用: !switch / !members / !mem / !hooks / !exit")
        return True

    def start(self) -> None:
        if self._script_mode:
            self._run_script()
        else:
            self._interactive()

    def _interactive(self) -> None:
        print(f"本地群聊模拟已启动。当前角色: {self.role}。成员: {self.members}")
        print("输入 `说话人: 内容` 发消息，如 `阿伟: 今天好冷`。输入 `exit` 退出。")
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            if line.startswith("!"):
                if not self._handle_command(line):
                    break
                continue
            if ":" in line or "：" in line:
                sender, text = line.split(":", 1) if ":" in line else line.split("：", 1)
                self._send(sender.strip(), text.strip())
            else:
                self._send(self.members[0], line)

    def _run_script(self) -> None:
        print(f"脚本模式，角色: {self.role}，共 {len(self._script_lines)} 条消息")
        for line in self._script_lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                self._handle_command(line)
                continue
            if ":" in line or "：" in line:
                sender, text = line.split(":", 1) if ":" in line else line.split("：", 1)
            else:
                sender, text = self.members[0], line
            self._send(sender.strip(), text.strip())
            print("─" * 40)

    def stop(self) -> None:
        pass


def main(argv=None):
    import argparse

    from ..runtime import build_runtime, resolve_project_root, resolve_path, start_local_shell

    parser = argparse.ArgumentParser(description="本地模拟群聊测试壳")
    parser.add_argument("--role", default="laomao", help="默认角色 id")
    parser.add_argument("--script", default=None, help="脚本文件路径（每行: 说话人: 内容）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--db", default="data/youchat.db", help="SQLite 路径")
    args = parser.parse_args(argv)

    project_root = resolve_project_root()
    cfg_path = resolve_path(project_root, args.config, "config.yaml")
    db_path = resolve_path(project_root, args.db, "data/youchat.db")

    try:
        runtime = build_runtime(project_root, cfg_path, db_path, args.role)
    except ValueError as e:
        print(e)
        sys.exit(1)

    script_path = Path(args.script) if args.script else None
    start_local_shell(runtime, args.role, script_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
