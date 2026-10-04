"""输入摘要：发布时冻结的确定性哈希。"""
from __future__ import annotations

import hashlib
import json


def canonical(value: object) -> str:
    """生成与键序无关的规范 JSON 串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    """对任意可序列化对象计算 sha256 摘要。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
