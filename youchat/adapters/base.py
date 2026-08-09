"""消息接入层抽象：引擎只依赖此接口，与具体 IM 平台解耦。

MVP 实现 local_shell；QQ/NapCat 预留。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Adapter(ABC):
    """接入层基类。实现方负责：接收消息、调用引擎、把回复发回群聊。"""

    def __init__(self, engine):
        self.engine = engine

    @abstractmethod
    def start(self) -> None:
        """启动接入（阻塞或起后台循环）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止接入，清理资源。"""
