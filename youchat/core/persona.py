"""静态人设定义加载与校验（Persona Core）。"""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import Character, Taboo


class PersonaError(Exception):
    pass


REQUIRED_FIELDS = ("character_id", "name", "personality", "speech_style")


def load_character(path: str | Path) -> Character:
    """从 yaml 加载一个角色定义，缺失必填字段时抛错。"""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PersonaError(f"{p}: 角色定义必须是 yaml 映射")

    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise PersonaError(f"{p}: 缺少必填字段 {', '.join(missing)}")

    return Character(
        character_id=str(data["character_id"]),
        name=str(data["name"]),
        personality=str(data.get("personality", "")),
        speech_style=str(data.get("speech_style", "")),
        background=str(data.get("background", "")),
        worldview=str(data.get("worldview", "")),
        taboos=[Taboo.from_value(t) for t in data.get("taboos", [])],
        reference_speech=str(data.get("reference_speech", "")),
    )


def load_characters_dir(directory: str | Path) -> dict[str, Character]:
    """加载 characters/ 目录下所有 *.yaml 角色，返回 {character_id: Character}。"""
    d = Path(directory)
    if not d.exists():
        return {}
    characters = {}
    for p in sorted(d.glob("*.yaml")):
        ch = load_character(p)
        characters[ch.character_id] = ch
    return characters
