"""版本化存储：关键实体只追加新版本，从不原地改写历史。"""
from __future__ import annotations

from copy import deepcopy
from typing import Callable

from .errors import ConflictError, NotFoundError


class VersionedStore:
    """同类实体的版本化存储。

    实体结构：{"id", "version", "data", "versions": [{version, at, by, reason, data}]}。
    versions[0] 为登记版本，之后每次变更追加一个完整快照。
    """

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._seq = 0
        self._items: dict[str, dict] = {}

    def create(self, data: dict, *, at: str, by: str, reason: str, entity_id: str | None = None) -> dict:
        self._seq += 1
        eid = entity_id or f"{self.prefix}-{self._seq:06d}"
        if eid in self._items:
            raise ConflictError(f"实体已存在：{eid}")
        entity = {
            "id": eid,
            "version": 1,
            "data": deepcopy(data),
            "versions": [{"version": 1, "at": at, "by": by, "reason": reason, "data": deepcopy(data)}],
        }
        self._items[eid] = entity
        return deepcopy(entity)

    def update(self, entity_id: str, changes: dict, *, at: str, by: str, reason: str) -> dict:
        entity = self._items.get(entity_id)
        if entity is None:
            raise NotFoundError(f"实体不存在：{entity_id}")
        data = {**entity["data"], **changes}
        entity["data"] = data
        entity["version"] += 1
        entity["versions"].append(
            {"version": entity["version"], "at": at, "by": by, "reason": reason, "data": deepcopy(data)}
        )
        return deepcopy(entity)

    def get(self, entity_id: str) -> dict:
        entity = self._items.get(entity_id)
        if entity is None:
            raise NotFoundError(f"实体不存在：{entity_id}")
        return deepcopy(entity)

    def get_or_none(self, entity_id: str) -> dict | None:
        entity = self._items.get(entity_id)
        return deepcopy(entity) if entity is not None else None

    def all(self) -> list[dict]:
        return [deepcopy(e) for e in self._items.values()]

    def find(self, predicate: Callable[[dict], bool]) -> list[dict]:
        return [e for e in self.all() if predicate(e)]
