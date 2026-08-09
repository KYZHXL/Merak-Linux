"""TUI：rich 终端交互面板（配置 / 角色 / 启动 / 切模式）。

三形态中唯一能真跑交互聊天的形态（有真实 stdin）。
启动落点：controller.start(role) → start_local_shell 前台跑 shell。
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..runtime import start_local_shell
from .controller import AppController, VALID_MODES

console = Console()


def _menu() -> str:
    table = Table(title="天璇 Merak 控制台", show_header=False, border_style="cyan")
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("1", "配置")
    table.add_row("2", "角色管理")
    table.add_row("3", "启动聊天")
    table.add_row("4", "切换界面模式")
    table.add_row("5", "查看记忆库")
    table.add_row("6", "QQ 接入")
    table.add_row("7", "AI 朋友（私聊）")
    table.add_row("0", "退出")
    console.print(table)
    return Prompt.ask("选择", choices=["0", "1", "2", "3", "4", "5", "6", "7"], default="0")


def _edit_config(controller: AppController) -> None:
    cfg = controller.get_config()
    console.print(Panel("配置（回车保留当前值）", border_style="green"))
    for section, fields in [
        ("model", ["base_url", "api_key", "chat_model", "embedding_model"]),
        ("storage", ["db_path", "characters_dir"]),
        ("engine", ["episodic_buffer_size", "compact_after", "max_recalled_memories"]),
        ("retrieval", ["vector_threshold", "semantic_slots"]),
    ]:
        console.print(f"\n[{section}]")
        for key in fields:
            cur = cfg.get(section, {}).get(key, "")
            val = Prompt.ask(f"  {key}", default=str(cur) if cur is not None else "")
            if key == "semantic_slots":
                cfg.setdefault(section, {})[key] = int(val)
            elif key == "vector_threshold":
                cfg.setdefault(section, {})[key] = float(val)
            elif key in ("episodic_buffer_size", "compact_after", "max_recalled_memories"):
                cfg.setdefault(section, {})[key] = int(val)
            else:
                cfg.setdefault(section, {})[key] = val
    res = controller.save_config(cfg)
    if res["ok"]:
        console.print("[green]配置已保存[/green]")
    else:
        for e in res["errors"]:
            console.print(f"[red]{e}[/red]")


def _edit_character(controller: AppController) -> None:
    roles = controller.list_characters()
    table = Table(title="角色列表", show_header=True)
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("文件")
    for r in roles:
        table.add_row(r["character_id"], r["name"], r["file"])
    console.print(table)
    cid = Prompt.ask("选择角色 ID（输入新的创建，空返回）", default="")
    if not cid:
        return
    data = controller.get_character(cid) or {"character_id": cid}
    console.print(Panel(f"编辑 {cid}（回车保留当前值）", border_style="green"))
    for field in ["name", "personality", "speech_style", "background", "worldview"]:
        cur = data.get(field, "")
        data[field] = Prompt.ask(f"  {field}", default=str(cur) if cur is not None else "")
    # taboos 编辑
    taboos = data.get("taboos", [])
    console.print("  [taboos] 当前条目（每行: text|kw1,kw2|example1;example2）")
    for i, t in enumerate(taboos):
        kws = ",".join(t.get("keywords", []))
        exs = ";".join(t.get("examples", []))
        console.print(f"    {i}: {t.get('text')} | {kws} | {exs}")
    if Confirm.ask("  修改 taboos？", default=False):
        new_taboos = []
        while True:
            line = Prompt.ask("  输入禁忌（text|kw1,kw2|ex1;ex2，空结束）", default="")
            if not line:
                break
            parts = line.split("|")
            text = parts[0].strip()
            if not text:
                continue
            kws = [k for k in parts[1].split(",") if k.strip()] if len(parts) > 1 else []
            exs = [e for e in parts[2].split(";") if e.strip()] if len(parts) > 2 else []
            new_taboos.append({"text": text, "keywords": kws, "examples": exs})
        data["taboos"] = new_taboos
    res = controller.save_character(cid, data)
    if res["ok"]:
        console.print("[green]角色已保存[/green]")
    else:
        for e in res["errors"]:
            console.print(f"[red]{e}[/red]")


def _start(controller: AppController) -> None:
    roles = [c["character_id"] for c in controller.list_characters()]
    if not roles:
        console.print("[red]没有可用角色[/red]")
        return
    role = Prompt.ask(f"选择角色（{', '.join(roles)}）", default=controller.get_settings().get("default_role", roles[0]))
    mock = Confirm.ask("用 Mock LLM（不调真实 API）？", default=False)
    res = controller.start(role, mock=mock)
    if not res["ok"]:
        console.print(f"[red]{res.get('error', '启动失败')}[/red]")
        return
    console.print("[green]引擎已启动，进入群聊模拟...[/green]（exit 返回菜单）")
    runtime = controller.runtime
    if runtime:
        start_local_shell(runtime, role)
        controller.runtime = None


def _switch_mode(controller: AppController) -> None:
    mode = Prompt.ask(f"选择界面模式（{', '.join(VALID_MODES)}）",
                      default=controller.get_settings().get("ui", {}).get("mode", "web"))
    res = controller.save_settings({"mode": mode})
    if res["ok"]:
        console.print(f"[green]已切换为 {mode}（重启 launcher 生效）[/green]")
    else:
        console.print(f"[red]{res.get('error', '失败')}[/red]")


def _view_memory(controller: AppController) -> None:
    files = controller.list_memory_files()
    if not files:
        console.print("[red]没有角色[/red]")
        return
    table = Table(title="角色记忆库", show_header=True)
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("大小")
    table.add_column("状态")
    for f in files:
        size = f"{f['size']/1000:.1f}KB" if f["exists"] else "-"
        status = "[green]有记忆[/green]" if f["exists"] else "[dim]无记忆[/dim]"
        table.add_row(f["character_id"], f["name"], size, status)
    console.print(table)
    cid = Prompt.ask("选择角色 ID（空返回）", default="")
    if not cid:
        return
    res = controller.get_memory(cid)
    if not res["ok"]:
        console.print(f"[yellow]{res.get('error', '无记忆')}[/yellow]")
        return
    console.print(Panel(res["content"], title=f"{cid} 记忆沉淀库", border_style="magenta"))


def _qq_setup(controller: AppController) -> None:
    settings = controller.get_settings().get("qq", {})
    roles = [c["character_id"] for c in controller.list_characters()]
    if not roles:
        console.print("[red]没有可用角色[/red]")
        return
    role = Prompt.ask(f"角色（{', '.join(roles)}）", default=controller.get_settings().get("default_role", roles[0]))
    bot_qq = Prompt.ask("机器人 QQ 号", default=str(settings.get("bot_qq", "")))
    ws_url = Prompt.ask("反向 WS 地址", default=str(settings.get("ws_url", "ws://127.0.0.1:6700")))
    mock = Confirm.ask("用 Mock LLM？", default=False)
    res = controller.start_qq(role, bot_qq, ws_url, mock=mock)
    if not res["ok"]:
        console.print(f"[red]{res.get('error', '启动失败')}[/red]")
        return
    console.print(f"[green]QQ 接入已启动[/green]（角色 {role}，@ 才回复）")
    console.print("回车停止...")
    Prompt.ask("输入回车停止", default="")
    controller.stop_qq()
    console.print("[yellow]QQ 接入已停止[/yellow]")


def _friend_setup(controller: AppController) -> None:
    settings = controller.get_settings().get("qq", {})
    roles = [c["character_id"] for c in controller.list_characters()]
    if not roles:
        console.print("[red]没有可用角色[/red]")
        return
    role = Prompt.ask(f"朋友角色（{', '.join(roles)}）", default=str(settings.get("friend_role") or roles[0]))
    bot_qq = Prompt.ask("机器人 QQ 号", default=str(settings.get("bot_qq", "")))
    ws_url = Prompt.ask("反向 WS 地址", default=str(settings.get("ws_url", "ws://127.0.0.1:6700")))
    mock = Confirm.ask("用 Mock LLM？", default=False)
    res = controller.start_friend(role, bot_qq, ws_url, mock=mock)
    if not res["ok"]:
        console.print(f"[red]{res.get('error', '启动失败')}[/red]")
        return
    console.print(f"[green]AI 朋友已启动[/green]（角色 {role}，私聊即回）")
    console.print("回车停止...")
    Prompt.ask("输入回车停止", default="")
    controller.stop_friend()
    console.print("[yellow]AI 朋友已停止[/yellow]")


def main(controller: AppController) -> None:
    console.print("[bold cyan]天璇 Merak 控制台[/bold cyan]（配置 / 角色 / 启动）")
    while True:
        choice = _menu()
        if choice == "0":
            controller.stop()
            break
        elif choice == "1":
            _edit_config(controller)
        elif choice == "2":
            _edit_character(controller)
        elif choice == "3":
            _start(controller)
        elif choice == "4":
            _switch_mode(controller)
        elif choice == "5":
            _view_memory(controller)
        elif choice == "6":
            _qq_setup(controller)
        elif choice == "7":
            _friend_setup(controller)
