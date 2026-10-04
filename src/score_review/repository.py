"""内存仓储：聚合全部版本化存储。"""
from __future__ import annotations

from threading import RLock

from .versioning import VersionedStore


class InMemoryRepository:
    """进程内仓储；接口与未来的持久化实现保持一致。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.rules = VersionedStore("RULE")
        self.evidence = VersionedStore("EV")
        self.rectifications = VersionedStore("RC")
        self.items = VersionedStore("DI")
        self.batches = VersionedStore("B")
        self.appeals = VersionedStore("AP")
        self.runs = VersionedStore("RUN")
        self.recusals: list[dict] = []
