"""规范化 JSON 与摘要算法：发布冻结与历史复算依赖确定性序列化。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(value: Any) -> str:
    """键排序、无空白、ensure_ascii=False 的确定性 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """计算规范化内容的 sha256 摘要。"""
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()
